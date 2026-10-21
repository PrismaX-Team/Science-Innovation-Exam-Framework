"""Host filesystem rules shared by the controller and container backends."""
import os
import subprocess
from pathlib import Path

from .errors import InfrastructureError

CONTAINER_UID = 65532


def writable(root):
    """Only container output directories are assigned to the container user."""
    root = Path(root)
    for path in [root] + list(root.rglob("*")):
        os.chown(str(path), CONTAINER_UID, CONTAINER_UID)
        path.chmod(0o770 if path.is_dir() else 0o660)


def require_disk(path):
    existing = Path(path).resolve()
    while not existing.exists():
        existing = existing.parent
    result = subprocess.run(["findmnt", "-n", "-o", "FSTYPE", "-T", str(existing)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True)
    if not result.stdout.strip() or any(kind in ("tmpfs", "ramfs") for kind in result.stdout.split()):
        raise InfrastructureError("experiment output must be on disk, not RAM: %s" % path)
