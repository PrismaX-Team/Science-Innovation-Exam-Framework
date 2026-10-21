"""Action transport 2: a directory mailbox inside the run directory.

Layout of ``<run>/control/``:
  requests/   agent-writable; the agent atomically renames ``<id>.json.tmp`` to ``<id>.json``
  responses/  controller-only; the agent polls for ``<id>.json``
  done/       controller-only; processed request files are moved here

The controller polls ``requests/`` every ``poll_seconds``. Files still carrying
a ``.tmp`` suffix are half-written and ignored. The response file name is the
request file name, so a malformed request still receives an error reply.
"""
import json
import re
import threading
from pathlib import Path

from ..common.host import writable
from ..common.serialization import atomic_write_bytes
from .interface import Channel, MountSpec

NAME = re.compile(r"^[A-Za-z0-9._-]{1,128}\.json$")


class MailboxChannel(Channel):
    def __init__(self, root, handler, poll_seconds=0.5, maximum_request_bytes=2_000_000):
        self.root, self.handler, self.poll_seconds = Path(root), handler, poll_seconds
        self.maximum = maximum_request_bytes
        self.requests, self.responses, self.done = self.root / "requests", self.root / "responses", self.root / "done"
        for path in (self.root, self.requests, self.responses, self.done):
            path.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o755)
        writable(self.requests)
        self.responses.chmod(0o755)
        self.done.chmod(0o700)
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        self.mounts = [MountSpec(self.root.name, "/control", False)]
        self.environment = {"SIE_ACTION_TRANSPORT": "mailbox", "SIE_MAILBOX": "/control"}

    def pending(self):
        names = sorted(p.name for p in self.requests.iterdir() if p.is_file() and NAME.match(p.name))
        return [name for name in names if not (self.responses / name).exists()]

    def serve_once(self):
        """Process at most one request; returns True when one was handled."""
        for name in self.pending():
            path = self.requests / name
            if path.is_symlink():
                raw = b"symbolic link"
            else:
                raw = path.read_bytes() if path.stat().st_size <= self.maximum else b"x" * (self.maximum + 1)
            response = self.handler(raw)
            payload = (json.dumps(response, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode()
            atomic_write_bytes(self.responses / name, payload, mode=0o644)
            try:
                path.replace(self.done / name)
            except OSError:
                pass
            return True
        return False

    def _serve(self):
        while not self.stop.is_set():
            try:
                while self.serve_once():
                    pass
            except Exception as error:  # a broken mailbox entry must not stop the controller
                (self.root / "channel-error.log").open("a").write("%s\n" % error)
            self.stop.wait(self.poll_seconds)

    def close(self):
        self.stop.set()
        self.thread.join(timeout=5 + self.poll_seconds)
