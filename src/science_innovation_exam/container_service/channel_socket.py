"""Action transport 1: one private Unix socket per experiment.

The socket is the only container-visible file outside the run directory: a
Unix socket cannot be created on shared or network storage, so it lives in a
short-lived RAM directory and is bind-mounted read-only at /control.
"""
import json
import os
import socketserver
import tempfile
import threading
from pathlib import Path

from ..common.host import CONTAINER_UID
from .interface import Channel, MountSpec


class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        self.connection.settimeout(10)
        raw = self.rfile.readline(self.server.maximum + 1)
        response = self.server.handler(raw)
        try:
            self.wfile.write((json.dumps(response, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode())
            self.wfile.flush()
        except OSError:
            pass  # a disconnected client already had its request processed


class _Server(socketserver.UnixStreamServer):
    """One serving thread is enough: task validation is serial by definition."""
    def __init__(self, path, handler, maximum):
        self.handler, self.maximum = handler, maximum
        super().__init__(str(path), _Handler)


class SocketChannel(Channel):
    def __init__(self, handler, maximum_request_bytes=2_000_000, directory="/tmp"):
        self.temporary = tempfile.TemporaryDirectory(prefix="sie-sock-", dir=directory)
        root = Path(self.temporary.name)
        os.chown(str(root), CONTAINER_UID, CONTAINER_UID)
        root.chmod(0o770)
        self.path = root / "verify.sock"
        self.server = _Server(self.path, handler, maximum_request_bytes)
        os.chown(str(self.path), CONTAINER_UID, CONTAINER_UID)
        self.path.chmod(0o660)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
        self.thread.start()
        self.mounts = [MountSpec(str(root), "/control", True)]
        self.environment = {"SIE_ACTION_TRANSPORT": "socket", "SIE_VERIFY_SOCKET": "/control/verify.sock"}

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.path.unlink(missing_ok=True)
        self.temporary.cleanup()
