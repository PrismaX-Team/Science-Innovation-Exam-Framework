"""Bounded protocol inspection and metadata snapshots; never retain model content."""
import json
import socket
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .common.serialization import atomic_write_bytes, canonical_json_bytes


class ProtocolProgress:
    """Recognize Responses / Chat SSE, ignoring comments, ping and in_progress.

    The bounded buffer is transient: no prompt or response content enters state.
    Unknown non-SSE protocols provide transport evidence only.
    """
    def __init__(self, content_type):
        self.sse = "text/event-stream" in content_type.lower()
        self.json = "application/json" in content_type.lower()
        self.known = self.sse
        self.buffer = b""
        self.data = []
        self.data_bytes = 0
        self.event_dropped = self.line_dropped = False
        self.complete = False
        self.failed = False
        self.created = False
        self.overflow = False

    def _event(self, raw):
        if raw.strip() == b"[DONE]":
            # Responses requires response.completed; [DONE] is not sufficient.
            return False
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            return False
        if not isinstance(value, dict):
            return False
        kind = value.get("type", "")
        if kind in ("response.failed", "response.incomplete", "error"):
            self.failed = True
            return True
        if kind == "response.completed":
            self.complete = True
            return True
        if kind == "response.created" and not self.created:
            self.created = True
            return True
        if isinstance(kind, str) and kind.startswith("response."):
            if kind.endswith(".delta"):
                return bool(value.get("delta"))
            return kind in ("response.output_item.added", "response.output_item.done",
                            "response.content_part.added", "response.content_part.done")
        choices = value.get("choices")
        if isinstance(choices, list):
            progress = False
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                if choice.get("finish_reason") is not None:
                    self.complete = True
                    progress = True
                delta = choice.get("delta") or {}
                if isinstance(delta, dict) and any(delta.get(k) for k in ("content", "tool_calls", "function_call")):
                    progress = True
            return progress
        return False

    def feed(self, chunk):
        if self.overflow or not (self.sse or self.json):
            return False
        self.buffer += chunk
        progress = False
        if self.sse:
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                line = line.rstrip(b"\r")
                if self.line_dropped:
                    self.line_dropped = False
                    continue
                if not line:
                    if self.data and not self.event_dropped:
                        progress = self._event(b"\n".join(self.data)) or progress
                    self.data = []
                    self.data_bytes = 0
                    self.event_dropped = False
                elif line.startswith(b"data:") and not self.event_dropped:
                    payload = line[5:].lstrip(b" ")
                    self.data_bytes += len(payload)
                    if self.data_bytes > 262144:
                        self.event_dropped = True
                        self.data = []
                    else:
                        self.data.append(payload)
        if len(self.buffer) > 262144:
            self.buffer = b""
            if self.sse:
                self.event_dropped = self.line_dropped = True
                self.data = []
            else:
                self.overflow = True
        return progress

    def finish(self):
        if self.json and not self.overflow:
            try:
                value = json.loads(self.buffer)
            except (ValueError, UnicodeError):
                self.failed = True
                self.known = True
                return
            if isinstance(value, dict) and value.get("status") == "completed":
                self.complete = True
                self.known = True
            elif isinstance(value, dict) and value.get("status") in ("failed", "incomplete"):
                self.failed = True
                self.known = True
            # A generic JSON API has no protocol-completion contract here.


def timeout_reason(options, elapsed, first, idle):
    if elapsed >= options["request_timeout_seconds"]:
        return "request_timeout"
    if not first and elapsed >= options["first_response_timeout_seconds"]:
        return "first_response_timeout"
    if first and idle >= options["idle_timeout_seconds"]:
        return "idle_timeout"
    return None


class GatewayActivity:
    """One writer coalesces progress; a separate watchdog interrupts blocked I/O."""
    def __init__(self, path, options, trace):
        self.path, self.options, self.trace = Path(path) if path else None, options, trace
        self.gateway_id = uuid.uuid4().hex
        self.lock = threading.RLock()
        self.changed, self.closed = threading.Event(), threading.Event()
        self.active, self.finished = {}, []
        self.failure = None
        self.sequence = 0
        self.writes = self.write_bytes = 0
        self.writer = threading.Thread(target=self._write_loop, daemon=True)
        self.watchdog = threading.Thread(target=self._watch_loop, daemon=True)
        self.writer.start()
        self.watchdog.start()

    def begin(self, request_id):
        with self.lock:
            self.active[request_id] = {"request_id": request_id, "started": time.monotonic(),
                                       "started_at": datetime.now(timezone.utc).isoformat(),
                                       "first": None, "progress": None, "progress_count": 0,
                                       "first_event_at": None, "last_progress_at": None,
                                       "attempts": 0, "http_status": None, "response_bytes": 0,
                                       "reason": None}
        self.changed.set()

    def update(self, request_id, **fields):
        with self.lock:
            self.active[request_id].update(fields)

    def progress(self, request_id, size, effective=False):
        with self.lock:
            row = self.active[request_id]
            row["response_bytes"] += size
            if effective:
                now = time.monotonic()
                row["first"] = row["first"] or now
                row["progress"] = now
                stamp = datetime.now(timezone.utc).isoformat()
                row["first_event_at"] = row["first_event_at"] or stamp
                row["last_progress_at"] = stamp
                row["progress_count"] += 1

    def reason(self, request_id):
        with self.lock:
            return self.active[request_id]["reason"]

    def _record(self, row, now):
        return {"request_id": row["request_id"], "elapsed_seconds": max(0, now - row["started"]),
                "first_event_seconds": None if row["first"] is None else row["first"] - row["started"],
                "progress_age_seconds": None if row["progress"] is None else max(0, now - row["progress"]),
                **{key: row[key] for key in ("started_at", "first_event_at", "last_progress_at",
                                             "progress_count", "attempts", "http_status", "response_bytes", "reason")}}

    def finish(self, request_id, reason, protocol_complete, transport_end):
        with self.lock:
            row = self.active.pop(request_id)
            row["reason"] = row["reason"] or reason
            record = self._record(row, time.monotonic())
            record.update(protocol_complete=protocol_complete, transport_end=transport_end,
                          upstream_cancellation="unknown")
            self.finished.append(record)
            self.finished = self.finished[-32:]
            # A caller may cancel normally (including controller stop); retain
            # evidence without claiming the model service failed.
            if row["reason"] not in (None, "client_disconnected") and self.failure is None:
                self.failure = record
        self.trace.write({"schema_version": "1.0", "event": "request.finished",
                          "time": datetime.now(timezone.utc).isoformat(), **record})
        self.changed.set()

    def _snapshot(self):
        with self.lock:
            self.sequence += 1
            now = time.monotonic()
            return {"schema_version": "1.0", "content_policy": "metadata-only", "gateway_id": self.gateway_id,
                    "sequence": self.sequence, "active": [self._record(r, now) for r in self.active.values()],
                    "finished": list(self.finished), "failure": self.failure}

    def _write_loop(self):
        def write():
            if self.path:
                payload = canonical_json_bytes(self._snapshot())
                atomic_write_bytes(self.path, payload)
                self.writes += 1
                self.write_bytes += len(payload)

        while not self.closed.is_set():
            self.changed.clear()
            write()
            self.changed.wait(self.options["progress_write_seconds"])
        write()

    def _watch_loop(self):
        while not self.closed.wait(self.options["check_seconds"]):
            if not self.options["enabled"]:
                continue
            with self.lock:
                now = time.monotonic()
                for row in self.active.values():
                    reason = timeout_reason(self.options, now - row["started"], row["first"] is not None,
                                            now - (row["progress"] or row["started"]))
                    if reason and not row["reason"]:
                        row["reason"] = reason
                        self.changed.set()
                        transport = row.get("socket")
                        if transport is not None:
                            try:
                                transport.shutdown(socket.SHUT_RDWR)
                            except OSError:
                                pass
                        # The external supervisor still enforces the deadline if connect/storage is stuck.

    def close(self):
        # Child exit or termination must also release any outstanding upstream socket.
        with self.lock:
            for row in self.active.values():
                row["reason"] = row["reason"] or "client_disconnected"
                transport = row.get("socket")
                if transport is not None:
                    try:
                        transport.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
        deadline = time.monotonic() + 1
        while self.active and time.monotonic() < deadline:
            time.sleep(0.01)
        self.closed.set()
        self.changed.set()
        self.watchdog.join(2)
        self.writer.join(2)
