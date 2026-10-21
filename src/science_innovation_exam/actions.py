"""The action protocol: serial calls, retry by request_id, test/abort are terminal.

Transports (Unix socket, directory mailbox) live in ``container_service`` and
only call ``serve_bytes``; this module never sees where a request came from.
"""
import json
import threading
import uuid

from .common.errors import ContractError, InfrastructureError
from .common.serialization import atomic_write_json


class Actions:
    def __init__(self, verifier, root, limits):
        self.verifier, self.root, self.limits = verifier, root, limits
        root.mkdir()
        self.lock = threading.Lock()
        self.done = threading.Event()
        self.test_requested = False
        self.abort_reason = None
        self.failure = None
        self.closed = False
        self.ordinal = 0
        self.cache = {}

    def handle(self, request):
        if not isinstance(request, dict) or set(request) != {"schema_version", "request_id", "action", "payload"}:
            raise ContractError("invalid action envelope")
        key = request["request_id"]
        if request["schema_version"] != "1.0" or not isinstance(key, str) or str(uuid.UUID(key)) != key:
            raise ContractError("invalid action version or request_id")
        if request["action"] not in ("validation", "test", "abort"):
            raise ContractError("action must be validation, test or abort")
        if request["action"] == "validation" and not isinstance(request["payload"], dict):
            raise ContractError("validation payload must be an object")
        if request["action"] == "test" and request["payload"] not in (None, {}):
            raise ContractError("test does not take a payload")
        if request["action"] == "abort":
            payload = request["payload"]
            if (not isinstance(payload, dict) or set(payload) != {"reason"}
                    or not isinstance(payload["reason"], str) or not payload["reason"].strip()):
                raise ContractError("abort payload must contain a non-empty reason string")
        # JSON comparison, not a cryptographic lock, is enough for retry identity.
        canonical = json.dumps(request, sort_keys=True, allow_nan=False)
        with self.lock:
            if key in self.cache:
                previous, response = self.cache[key]
                if previous != canonical:
                    raise ContractError("request_id was reused with different content")
                return response
            if self.closed:
                raise ContractError("experiment has ended")
            response = {"schema_version": "1.0", "request_id": key, "action": request["action"],
                        "ok": True, "terminal": request["action"] in ("test", "abort")}
            self.ordinal += 1
            if request["action"] in ("test", "abort"):
                if request["action"] == "test":
                    self.test_requested = True
                else:
                    self.abort_reason = request["payload"]["reason"]
                self.closed = True
                response["accepted"] = True
            else:
                directory = self.root / ("validation-%04d" % self.ordinal)
                try:
                    response["response"] = self.verifier.invoke("validation", directory, request=request["payload"])
                except Exception as error:
                    status = "infrastructure_failed" if isinstance(error, InfrastructureError) else "validation_failed"
                    self.failure = {"status": status, "type": type(error).__name__, "error": str(error)}
                    self.closed = True
                    response.update(ok=False, terminal=True, error="validation execution failed; see host logs")
            atomic_write_json(self.root / (key + ".json"), {"request": request, "response": response})
            self.cache[key] = (canonical, response)
            return response

    def serve_bytes(self, raw):
        """Transport entry point: one encoded request in, one response dict out.

        The terminal flag is recorded here, before any transport tries to
        deliver the reply, so a disconnected client cannot stall the run."""
        try:
            if len(raw) > self.limits["request_bytes"]:
                raise ContractError("action request is too large")
            response = self.handle(json.loads(raw))
        except (ValueError, ContractError, OSError) as error:
            response = {"ok": False, "terminal": False, "error": str(error)}
        if response.get("terminal"):
            self.done.set()
        return response
