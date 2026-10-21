"""Small persistent state shared by one task's validation and test entries.

The trusted framework creates one context directory for each benchmark Run.  Task
code opens the directory through :meth:`VerifyContext.current`; it never chooses
the directory or a Run identity itself.
"""

import json
import os
import sqlite3
import stat
import sys
from contextlib import closing
from pathlib import Path
from typing import Any, Iterator, Mapping, MutableMapping

from .common.errors import ContractError, SecurityBoundaryError
from .common.serialization import canonical_json_bytes


VERIFY_CONTEXT_ENV = "SIE_VERIFY_CONTEXT"
VERIFY_CONTEXT_DATABASE = "state.sqlite3"
VERIFY_CONTEXT_SCHEMA_VERSION = "1"


def _validate_key(key: str) -> str:
    if not isinstance(key, str) or not key or "\x00" in key:
        raise ContractError("VerifyContext keys must be non-empty, NUL-free strings")
    return key


def _encode_value(value: Any) -> bytes:
    try:
        return canonical_json_bytes(value)
    except (TypeError, ValueError) as error:
        raise ContractError("VerifyContext values must be finite JSON values") from error


class VerifyContext(MutableMapping):
    """A JSON key/value space shared by validation and final test code.

    Each operation is committed atomically by SQLite.  The framework serializes
    validation invocations belonging to one Run, so familiar mapping operations
    are sufficient for task authors.  The tested Agent never receives this path.
    """

    def __init__(self, root: Path) -> None:
        selected = Path(root)
        if selected.is_symlink() or not selected.is_dir():
            raise SecurityBoundaryError("VerifyContext root must be a real directory")
        self.root = selected.resolve()
        self.database_path = self.root / VERIFY_CONTEXT_DATABASE
        try:
            info = self.database_path.lstat()
        except FileNotFoundError as error:
            raise ContractError("VerifyContext database does not exist") from error
        if not stat.S_ISREG(info.st_mode) or self.database_path.is_symlink():
            raise SecurityBoundaryError("VerifyContext database must be a real file")
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT schema_version, maximum_bytes FROM verify_context_metadata WHERE singleton=1"
            ).fetchone()
        if row is None or str(row[0]) != VERIFY_CONTEXT_SCHEMA_VERSION:
            raise ContractError("VerifyContext database has an unsupported schema")
        self._maximum_bytes = int(row[1])
        if self._maximum_bytes < 1:
            raise ContractError("VerifyContext maximum_bytes must be positive")

    @classmethod
    def create(cls, root: Path, maximum_bytes: int) -> "VerifyContext":
        """Create the one context owned by a new benchmark Run."""

        if (
            isinstance(maximum_bytes, bool)
            or not isinstance(maximum_bytes, int)
            or maximum_bytes < 1
        ):
            raise ValueError("VerifyContext maximum_bytes must be positive")
        selected = Path(root)
        if selected.exists() or selected.is_symlink():
            raise ContractError("VerifyContext root already exists: %s" % selected)
        selected.mkdir(parents=True, mode=0o700)
        selected.chmod(0o700)
        database_path = selected / VERIFY_CONTEXT_DATABASE
        connection = sqlite3.connect(str(database_path), isolation_level=None)
        try:
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                CREATE TABLE verify_context_metadata (
                    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                    schema_version TEXT NOT NULL,
                    maximum_bytes INTEGER NOT NULL CHECK (maximum_bytes > 0)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE verify_context_state (
                    key TEXT PRIMARY KEY,
                    value_json TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL CHECK (size_bytes >= 0)
                )
                """
            )
            connection.execute(
                "INSERT INTO verify_context_metadata(singleton, schema_version, maximum_bytes) VALUES(1, ?, ?)",
                (VERIFY_CONTEXT_SCHEMA_VERSION, int(maximum_bytes)),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
        database_path.chmod(0o600)
        return cls(selected)

    @classmethod
    def current(cls) -> "VerifyContext":
        """Open the context injected into a test or validation process."""

        value = os.environ.get(VERIFY_CONTEXT_ENV)
        if not value:
            raise ContractError("%s is not set" % VERIFY_CONTEXT_ENV)
        return cls(Path(value))

    @property
    def maximum_bytes(self) -> int:
        return self._maximum_bytes

    @property
    def state(self) -> "VerifyContext":
        """Allow the equally supported ``context.state[key]`` spelling."""

        return self

    def log(self, value: Any) -> None:
        """Write one JSON line to the invocation log captured by the framework.

        Content belongs to the task. Logs are diagnostic output, not persistent
        context state, scoring results, or feedback returned to the tested Agent.
        The runner owns the output file and its configured byte limit.
        """

        sys.stdout.write(_encode_value(value).decode("utf-8"))
        sys.stdout.flush()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.database_path), timeout=30.0, isolation_level=None)
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def __getitem__(self, key: str) -> Any:
        selected = _validate_key(key)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT value_json FROM verify_context_state WHERE key=?", (selected,)
            ).fetchone()
        if row is None:
            raise KeyError(selected)
        try:
            return json.loads(str(row[0]))
        except json.JSONDecodeError as error:
            raise ContractError("VerifyContext contains malformed JSON") from error

    def __setitem__(self, key: str, value: Any) -> None:
        selected = _validate_key(key)
        encoded = _encode_value(value)
        text = encoded.decode("utf-8")
        size_bytes = len(selected.encode("utf-8")) + len(encoded)
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                previous = connection.execute(
                    "SELECT size_bytes FROM verify_context_state WHERE key=?", (selected,)
                ).fetchone()
                total = int(
                    connection.execute(
                        "SELECT COALESCE(SUM(size_bytes), 0) FROM verify_context_state"
                    ).fetchone()[0]
                )
                projected = total - (int(previous[0]) if previous is not None else 0) + size_bytes
                if projected > self.maximum_bytes:
                    raise ContractError(
                        "VerifyContext JSON payload would exceed its configured byte limit"
                    )
                connection.execute(
                    """
                    INSERT INTO verify_context_state(key, value_json, size_bytes)
                    VALUES(?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value_json=excluded.value_json,
                        size_bytes=excluded.size_bytes
                    """,
                    (selected, text, size_bytes),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def __delitem__(self, key: str) -> None:
        selected = _validate_key(key)
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    "DELETE FROM verify_context_state WHERE key=?", (selected,)
                )
                if cursor.rowcount != 1:
                    raise KeyError(selected)
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    def __iter__(self) -> Iterator[str]:
        with closing(self._connect()) as connection:
            keys = tuple(
                str(row[0])
                for row in connection.execute(
                    "SELECT key FROM verify_context_state ORDER BY key"
                ).fetchall()
            )
        return iter(keys)

    def __len__(self) -> int:
        with closing(self._connect()) as connection:
            return int(connection.execute("SELECT COUNT(*) FROM verify_context_state").fetchone()[0])

    def snapshot(self) -> Mapping[str, Any]:
        """Return a stable ordinary mapping for logging, testing, or final scoring."""

        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT key, value_json FROM verify_context_state ORDER BY key"
            ).fetchall()
        try:
            return {str(key): json.loads(str(value)) for key, value in rows}
        except json.JSONDecodeError as error:
            raise ContractError("VerifyContext contains malformed JSON") from error

    def replace_all(self, values: Mapping[str, Any]) -> None:
        """Atomically replace all state, primarily for framework rollback."""

        if not isinstance(values, Mapping):
            raise ContractError("VerifyContext replacement must be a mapping")
        prepared = []
        total = 0
        for key, value in tuple(values.items()):
            selected = _validate_key(key)
            encoded = _encode_value(value)
            size_bytes = len(selected.encode("utf-8")) + len(encoded)
            total += size_bytes
            prepared.append((selected, encoded.decode("utf-8"), size_bytes))
        if total > self.maximum_bytes:
            raise ContractError(
                "VerifyContext replacement would exceed its configured byte limit"
            )
        with closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("DELETE FROM verify_context_state")
                connection.executemany(
                    "INSERT INTO verify_context_state(key, value_json, size_bytes) VALUES(?, ?, ?)",
                    prepared,
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
