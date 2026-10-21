"""Controller-side supervision using only metadata and the controller's monotonic clock."""
import json
import math
import threading
import time

from .common.errors import InfrastructureError
from .common.serialization import atomic_write_json
from .model_activity import timeout_reason


class ModelFault(InfrastructureError):
    def __init__(self, phase, reason, request_id=None):
        super().__init__(reason)
        self.phase, self.reason, self.request_id = phase, reason, request_id


def validate_snapshot(value):
    """Remote metadata is untrusted. Reject malformed, unbounded or non-finite state."""
    if not isinstance(value, dict) or value.get("schema_version") != "1.0":
        raise ValueError("invalid activity schema")
    if not isinstance(value.get("gateway_id"), str) or not value["gateway_id"]:
        raise ValueError("missing gateway identity")
    if type(value.get("sequence")) is not int or value["sequence"] < 1:
        raise ValueError("invalid activity sequence")
    if not isinstance(value.get("active"), list) or not isinstance(value.get("finished"), list):
        raise ValueError("invalid request lists")
    rows = value["active"] + value["finished"]
    if value.get("failure") is not None:
        rows.append(value["failure"])
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("request_id"), str):
            raise ValueError("invalid request metadata")
        for key in ("elapsed_seconds", "first_event_seconds", "progress_age_seconds"):
            number = row.get(key)
            if key != "elapsed_seconds" and number is None:
                continue
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or number < 0:
                raise ValueError("invalid request duration")
        if row.get("reason") is not None and not isinstance(row["reason"], str):
            raise ValueError("invalid request reason")
    return value


class ModelMonitor:
    def __init__(self, service, root, options):
        self.service, self.root, self.options = service, root, options
        self.started = self.last_fresh = time.monotonic()
        self.next_check = self.started
        self.identity = None
        self.sequence = 0
        self.latest = None
        self.received = None
        self.warned = set()
        self.last_observation_error = None
        self.checks = 0
        self.query = None

    def read_snapshot(self):
        # One outstanding read per run, even if a backend or local filesystem
        # ignores its timeout. A blocked observer must not block controller stop.
        if self.query is None:
            done, result = threading.Event(), {}

            def read():
                try:
                    result['value'] = self.service.read_model_activity(self.options['query_timeout_seconds'])
                except Exception as error:
                    result['error'] = error
                finally:
                    done.set()

            self.query = done, result
            threading.Thread(target=read, daemon=True).start()
        done, result = self.query
        if not done.wait(self.options['query_timeout_seconds']):
            raise TimeoutError('model activity query exceeded deadline')
        self.query = None
        if 'error' in result:
            raise result['error']
        return result['value']

    def event(self, kind, **fields):
        # No exception text from network libraries: it may contain credentials or content.
        record = {"event": kind, "elapsed_seconds": round(time.monotonic() - self.started, 3), **fields}
        with (self.root / "model-monitor.jsonl").open("a", encoding="utf-8") as output:
            output.write(json.dumps(record, sort_keys=True) + "\n")

    def warn(self, reason, request_id=None):
        key = (reason, request_id)
        if key not in self.warned:
            self.warned.add(key)
            self.event("warning", reason=reason, request_id=request_id)

    def fail(self, phase, reason, request_id=None):
        if phase == "model_observation":
            self.last_observation_error = reason
        self.event("terminated", phase=phase, reason=reason, request_id=request_id)
        raise ModelFault(phase, reason, request_id)

    def check(self, force=False, final=False):
        now = time.monotonic()
        if not force and now < self.next_check:
            return
        self.next_check = now + self.options["check_seconds"]
        self.checks += 1
        try:
            snapshot = validate_snapshot(self.read_snapshot())
            now = time.monotonic()
            self.last_observation_error = None
            if self.identity is not None and snapshot["gateway_id"] != self.identity:
                self.fail("model_observation", "gateway_restarted")
            if self.identity is None or snapshot["sequence"] > self.sequence:
                self.identity, self.sequence = snapshot["gateway_id"], snapshot["sequence"]
                self.latest, self.received, self.last_fresh = snapshot, now, now
                self.last_observation_error = None
                atomic_write_json(self.root / "model-activity-observed.json", snapshot)
            elif snapshot["sequence"] < self.sequence:
                self.last_observation_error = "activity_sequence_regressed"
        except ModelFault:
            raise
        except Exception as error:
            self.last_observation_error = type(error).__name__
            now = time.monotonic()
        stale = now - self.last_fresh
        if final and self.last_observation_error is not None:
            self.fail("model_observation", "model_observation_unavailable")
        if not final and stale >= self.options["observation_warning_seconds"]:
            self.warn("model_observation_unavailable")
        if not final and stale >= self.options["observation_timeout_seconds"]:
            self.fail("model_observation", "model_observation_unavailable")
        if self.latest is None:
            return
        failure = self.latest.get("failure")
        if failure:
            self.fail("model_api", failure["reason"] or "model_request_failed", failure["request_id"])
        # Ages originate in one remote monotonic clock; only add locally elapsed time.
        # Never compare wall clocks or subtract clocks from different machines.
        lag = now - self.received
        for row in self.latest["active"]:
            if row.get("reason") == "client_disconnected":
                continue
            elapsed = row["elapsed_seconds"] + lag
            first = row["first_event_seconds"] is not None
            idle = (row["progress_age_seconds"] or 0) + lag
            request_id = row["request_id"]
            if elapsed >= self.options["request_warning_seconds"]:
                self.warn("request_wait", request_id)
            if not first and elapsed >= self.options["first_response_warning_seconds"]:
                self.warn("first_response_wait", request_id)
            if first and idle >= self.options["idle_warning_seconds"]:
                self.warn("request_no_progress", request_id)
            reason = row.get("reason") or timeout_reason(self.options, elapsed, first, idle)
            if reason:
                if self.last_observation_error or stale >= self.options["observation_warning_seconds"]:
                    self.fail("model_observation", "model_observation_unavailable", request_id)
                self.fail("model_api", reason, request_id)

    def summary(self):
        return {"checks": self.checks, "warnings": len(self.warned), "observed": self.latest is not None,
                "observation_status": "unavailable" if self.latest is None or self.last_observation_error else "observed",
                "last_observation_error": self.last_observation_error,
                "scope": "framework_model_gateway"}
