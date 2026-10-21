import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Mapping

from ..common.serialization import atomic_write_json
from .errors import MatrixContractError, MatrixScoringError
from .models import CustomUtilityEvaluator
from .transforms import decimal_value


CUSTOM_ENTRY_PARTS = ("verifier", "matrix", "transform.py")


class DevelopmentPythonTransformRunner(CustomUtilityEvaluator):
    """Run package-owned Python in a separate host process for development only.

    Official callers must execute the same worker protocol in the offline,
    resource-limited verifier container. A host subprocess is not a security
    boundary and this class deliberately makes no official-mode claim.
    """

    def __init__(
        self,
        transform_path: Path,
        python_executable: str,
        timeout_seconds: int = 10,
        maximum_result_bytes: int = 1024 * 1024,
        maximum_log_bytes: int = 1024 * 1024,
    ) -> None:
        unresolved_transform = transform_path.absolute()
        if unresolved_transform.is_symlink():
            raise MatrixContractError("custom Matrix entry cannot be a symlink")
        self.transform_path = unresolved_transform.resolve()
        self.python_executable = python_executable
        self.timeout_seconds = timeout_seconds
        self.maximum_result_bytes = maximum_result_bytes
        self.maximum_log_bytes = maximum_log_bytes
        if tuple(self.transform_path.parts[-3:]) != CUSTOM_ENTRY_PARTS:
            raise MatrixContractError(
                "custom Matrix entry must be verifier/matrix/transform.py"
            )
        if (
            not self.transform_path.is_file()
            or self.transform_path.is_symlink()
            or self.transform_path.stat().st_size == 0
        ):
            raise MatrixContractError("custom Matrix entry is missing, unsafe, or empty")
        if not python_executable or "\x00" in python_executable:
            raise ValueError("python_executable must be non-empty and NUL-free")
        if min(timeout_seconds, maximum_result_bytes, maximum_log_bytes) < 1:
            raise ValueError("custom Matrix process limits must be positive")

    def evaluate(
        self,
        raw_values: tuple,
        parameters: Mapping[str, Any],
        precision: int,
    ) -> tuple:
        selected = tuple(decimal_value(item, "custom raw value") for item in raw_values)
        worker = Path(__file__).with_name("worker.py").resolve()
        with tempfile.TemporaryDirectory(prefix="sie-matrix-") as temporary:
            root = Path(temporary)
            request_path = root / "request.json"
            result_path = root / "result.json"
            stdout_path = root / "stdout.log"
            stderr_path = root / "stderr.log"
            atomic_write_json(
                request_path,
                {
                    "schema_version": "1.0",
                    "raw_values": [
                        str(item.copy_abs() if item.is_zero() else item)
                        for item in selected
                    ],
                    "parameters": dict(parameters),
                    "precision": precision,
                },
            )
            environment = {
                "LANG": "C.UTF-8",
                "LC_ALL": "C.UTF-8",
                "PATH": os.environ.get(
                    "PATH", "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
                ),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONHASHSEED": "0",
            }
            argv = (
                self.python_executable,
                "-I",
                str(worker),
                "--transform",
                str(self.transform_path),
                "--request",
                str(request_path),
                "--result",
                str(result_path),
            )
            try:
                with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
                    completed = subprocess.run(
                        argv,
                        cwd=str(self.transform_path.parent),
                        env=environment,
                        stdin=subprocess.DEVNULL,
                        stdout=stdout,
                        stderr=stderr,
                        timeout=self.timeout_seconds,
                        check=False,
                    )
            except subprocess.TimeoutExpired:
                raise MatrixScoringError("custom Matrix transform timed out")
            if stdout_path.stat().st_size > self.maximum_log_bytes:
                raise MatrixScoringError("custom Matrix stdout exceeds the configured limit")
            if stderr_path.stat().st_size > self.maximum_log_bytes:
                raise MatrixScoringError("custom Matrix stderr exceeds the configured limit")
            if completed.returncode != 0:
                detail = stderr_path.read_text(encoding="utf-8", errors="replace")[-2000:]
                raise MatrixScoringError("custom Matrix transform failed: %s" % detail)
            if not result_path.is_file() or result_path.is_symlink():
                raise MatrixScoringError("custom Matrix worker did not produce a safe result")
            if result_path.stat().st_size > self.maximum_result_bytes:
                raise MatrixScoringError("custom Matrix result exceeds the configured limit")
            try:
                response = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
                raise MatrixScoringError("cannot read custom Matrix result: %s" % error)
            if not isinstance(response, Mapping) or set(response) != {
                "schema_version",
                "status",
                "utility_values",
            }:
                raise MatrixScoringError("custom Matrix worker returned an invalid result object")
            if response["schema_version"] != "1.0" or response["status"] != "ok":
                raise MatrixScoringError("custom Matrix worker returned an unsuccessful result")
            values = response["utility_values"]
            if not isinstance(values, list) or len(values) != len(selected):
                raise MatrixScoringError("custom Matrix worker returned the wrong utility count")
            return tuple(
                decimal_value(item, "custom utility", strings_only=True)
                for item in values
            )
