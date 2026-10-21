import hashlib
import json
from pathlib import Path
from typing import Any, BinaryIO, Iterable, Mapping


CHUNK_BYTES = 1024 * 1024


def canonical_json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_stream(handle: BinaryIO) -> str:
    digest = hashlib.sha256()
    for chunk in iter(lambda: handle.read(CHUNK_BYTES), b""):
        digest.update(chunk)
    return digest.hexdigest()


def sha256_file(path: Path) -> str:
    with path.open("rb") as handle:
        return sha256_stream(handle)


def sha256_mapping(value: Mapping[str, Any]) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def normalized_sha256(value: str) -> str:
    candidate = value.casefold()
    if candidate.startswith("sha256:"):
        candidate = candidate[7:]
    if len(candidate) != 64 or any(character not in "0123456789abcdef" for character in candidate):
        raise ValueError("value is not a SHA-256 digest")
    return candidate


def tree_digest(entries: Iterable[Mapping[str, Any]]) -> str:
    """Hash a sorted file inventory without depending on filesystem metadata."""
    normalized = sorted(
        (
            {
                "path": str(entry["path"]),
                "sha256": normalized_sha256(str(entry["sha256"])),
                "size_bytes": int(entry["size_bytes"]),
            }
            for entry in entries
        ),
        key=lambda item: item["path"],
    )
    return sha256_bytes(canonical_json_bytes(normalized))

