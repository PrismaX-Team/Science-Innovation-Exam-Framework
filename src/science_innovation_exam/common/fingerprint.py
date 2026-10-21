"""Content fingerprint of a directory tree.

The digest covers every regular file's relative path and bytes, plus empty
directory paths. It identifies a task package or an agent program so a shared
copy can be reused. It is not a signature and it does not follow links.
"""
import hashlib
from pathlib import Path

from .errors import ContractError


def _sha256(path):
    hasher = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(block)
    return hasher.digest()


def content_fingerprint(root):
    root = Path(root).resolve()
    if not root.is_dir():
        raise ContractError("fingerprint source must be a directory: %s" % root)
    records = []
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise ContractError("fingerprint source cannot contain links: %s" % relative)
        if path.is_dir():
            if not any(path.iterdir()):
                records.append((relative + "/", b""))  # Trailing slash distinguishes directory records from files.
            continue
        if not path.is_file():
            raise ContractError("fingerprint source must contain only regular files: %s" % relative)
        records.append((relative, _sha256(path)))
    hasher = hashlib.sha256()
    for relative, digest in sorted(records):
        hasher.update(relative.encode())
        hasher.update(b"\0")
        hasher.update(digest)
        hasher.update(b"\n")
    return hasher.hexdigest()
