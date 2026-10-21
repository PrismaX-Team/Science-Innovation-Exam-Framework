"""Local metadata gateway for agents that call a model.

The process listens on 127.0.0.1, forwards each request to the configured
upstream, and appends one metadata record per call. The record is the time,
request id, byte counts, body fingerprint, model name, whether the caller
asked for a stream, and the HTTP status. Prompt text and model replies are
not written.

An optional adapter module may rewrite the JSON request before it is
forwarded. The adapter is agent-specific. A caller whose requests are already
acceptable passes no adapter.

Only agents that call a model start this process. A scripted agent does not.
"""
import argparse
import gzip
import hashlib
import importlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.client import HTTPConnection, HTTPSConnection, IncompleteRead, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPHandler, HTTPSHandler, ProxyHandler, Request, build_opener

from .model_monitor_config import MONITOR_DEFAULTS, check_model_monitor
from .model_activity import GatewayActivity, ProtocolProgress


LOCAL_PORT = 18765
MAX_REQUEST_BYTES = 32 * 1024 * 1024
_HOP_BY_HOP = {
    "connection", "content-length", "host", "keep-alive", "proxy-authenticate",
    "proxy-authorization", "proxy-connection", "te", "trailer", "transfer-encoding", "upgrade",
    "content-encoding",
}


class GatewayError(ValueError):
    pass


def normalize_upstream(value):
    parsed = urlsplit(value.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise GatewayError("upstream base URL must be HTTP(S)")
    if parsed.username is not None or parsed.password is not None:
        raise GatewayError("upstream base URL must not contain credentials")
    if parsed.query or parsed.fragment:
        raise GatewayError("upstream base URL must not contain query or fragment")
    path = parsed.path.rstrip("/")
    if not path.endswith("/v1"):
        raise GatewayError("upstream base URL must end with /v1")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def upstream_url(base, target):
    base_parts = urlsplit(base)
    incoming = urlsplit(target)
    if incoming.scheme or incoming.netloc or incoming.fragment:
        raise GatewayError("request target must be origin-form")
    if incoming.path == "/v1":
        suffix = ""
    elif incoming.path.startswith("/v1/"):
        suffix = incoming.path[3:]
    else:
        raise GatewayError("request path must remain below /v1")
    return urlunsplit((base_parts.scheme, base_parts.netloc, base_parts.path + suffix, incoming.query, ""))


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _decode_json(body, encoding):
    encoding = (encoding or "").strip().lower()
    if encoding in ("", "identity"):
        raw = body
    elif encoding == "gzip":
        try:
            raw = gzip.decompress(body)
        except (OSError, EOFError) as error:
            raise GatewayError("invalid gzip request body") from error
    else:
        raise GatewayError("unsupported request content encoding")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise GatewayError("request body must be a UTF-8 JSON object") from error
    if not isinstance(value, dict):
        raise GatewayError("request body must be a JSON object")
    return value


def _encode_json(document):
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")


class _Trace:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("a", encoding="utf-8")
        self.lock = threading.Lock()
        self.forwarded = 0
        self.statuses = {}

    def write(self, event):
        line = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self.lock:
            if self.handle.closed:
                return
            self.handle.write(line + "\n")
            self.handle.flush()
            if event.get("event") == "request.forwarded":
                self.forwarded += 1
            if event.get("event") == "request.finished" and isinstance(event.get("http_status"), int):
                key = str(event["http_status"])
                self.statuses[key] = self.statuses.get(key, 0) + 1

    def close(self):
        with self.lock:
            self.handle.close()

    def summary(self):
        return {
            "schema_version": "1.0",
            "content_policy": "metadata-only",
            "forwarded": self.forwarded,
            "http_status_counts": self.statuses,
        }


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, upstream, timeout_seconds, trace, adapter, activity=None):
        self.upstream = upstream
        self.timeout_seconds = timeout_seconds
        self.trace = trace
        self.adapter = adapter
        self.activity = activity
        super().__init__(address, _Handler)


def _opener(activity, request_id):
    # Keep urllib's proxy/TLS behavior, but expose the actual socket to the deadline watchdog.
    def factory(base):
        class Connection(base):
            def connect(self):
                super().connect()
                activity.update(request_id, socket=self.sock)
                # connect_timeout is not a read/idle budget. The watchdog enforces
                # first-response, progress and total deadlines across all reads.
                self.sock.settimeout(activity.options["request_timeout_seconds"]
                                     if activity.options["enabled"] else None)
        return Connection

    class HTTP(HTTPHandler):
        def http_open(self, request):
            return self.do_open(factory(HTTPConnection), request)

    class HTTPS(HTTPSHandler):
        def https_open(self, request):
            return self.do_open(factory(HTTPSConnection), request, context=self._context)

    return build_opener(ProxyHandler(), HTTP(), HTTPS())


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    server_version = "SIEModelGateway/1"
    sys_version = ""

    def log_message(self, _format, *_args):
        return

    def _error(self, status, code):
        body = _encode_json({"error": {"code": code}})
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        self.close_connection = True

    def do_POST(self):
        request_id = "sie-" + uuid.uuid4().hex
        try:
            length = int(self.headers.get("Content-Length") or "")
        except ValueError:
            self._error(411, "content_length_required")
            return
        if length <= 0 or length > MAX_REQUEST_BYTES:
            self._error(413, "request_size")
            return
        try:
            body = self.rfile.read(length)
            if len(body) != length:
                raise GatewayError("request body ended early")
            document = _decode_json(body, self.headers.get("Content-Encoding", ""))
            patch_count = 0
            if self.server.adapter is not None:
                document, patch_count = self.server.adapter(document)
                if not isinstance(document, dict) or isinstance(patch_count, bool) or not isinstance(patch_count, int):
                    raise GatewayError("adapter must return a JSON object and a patch count")
            forwarded = _encode_json(document)
            target = upstream_url(self.server.upstream, self.path)
        except GatewayError:
            self._error(400, "rejected_request")
            return
        model = document.get("model") if isinstance(document.get("model"), str) else None
        stream = document.get("stream") is True
        activity = self.server.activity
        activity.begin(request_id)
        self.server.trace.write({
            "schema_version": "1.0",
            "event": "request.forwarded",
            "time": _now(),
            "request_id": request_id,
            "request_bytes": len(forwarded),
            "payload_sha256": hashlib.sha256(forwarded).hexdigest(),
            "model": model,
            "stream": stream,
            "patch_count": patch_count,
        })
        headers = {}
        for name, value in self.headers.items():
            if name.lower() not in _HOP_BY_HOP:
                headers[name] = value
        headers["Content-Type"] = "application/json"
        headers["Accept-Encoding"] = "identity"
        request = Request(target, data=forwarded, headers=headers, method="POST")
        options = activity.options
        response = None
        began = time.monotonic()
        reason = None
        for attempt in range(options["retry_attempts"] + 1):
            if activity.reason(request_id):
                break
            activity.update(request_id, attempts=attempt + 1)
            try:
                try:
                    remaining = (max(0.001, options["request_timeout_seconds"] - (time.monotonic() - began))
                                 if options["enabled"] else self.server.timeout_seconds)
                    response = _opener(activity, request_id).open(
                        request, timeout=min(self.server.timeout_seconds, remaining))
                except HTTPError as error:
                    response = error
                status = int(response.getcode())
                activity.update(request_id, http_status=status)
                if status < 400:
                    break
                reason = "upstream_http_error"
                if status not in (429, 502, 503, 504) or attempt == options["retry_attempts"]:
                    break
                response.close()
                response = None
            except (URLError, HTTPException, TimeoutError, OSError):
                reason = "upstream_connection_error"
                if attempt == options["retry_attempts"]:
                    break
            # Only retry the model HTTP request before delivering a response, never tools/actions.
            until = time.monotonic() + options["retry_backoff_seconds"] * (2 ** min(attempt, 16))
            while time.monotonic() < until and not activity.reason(request_id):
                time.sleep(max(0, min(0.05, until - time.monotonic())))
        if response is None:
            activity.finish(request_id, reason or "upstream_connection_error", False, "connection_error")
            self._error(502, "upstream_connection")
            return
        status = int(response.getcode())
        reason = "upstream_http_error" if status >= 400 else None
        protocol = ProtocolProgress(response.headers.get("Content-Type", ""))
        transport_end = "eof"
        try:
            try:
                self.send_response(status, response.reason)
                for name, value in response.headers.items():
                    if name.lower() not in _HOP_BY_HOP:
                        self.send_header(name, value)
                self.send_header("Connection", "close")
                self.end_headers()
            except (BrokenPipeError, ConnectionResetError, OSError):
                reason, transport_end = "client_disconnected", "client_disconnect"
            else:
                reader = getattr(response, "read1", response.read)
                while True:
                    try:
                        chunk = reader(64 * 1024)
                    except (IncompleteRead, HTTPException, OSError):
                        reason, transport_end = "upstream_stream_error", "read_error"
                        break
                    if not chunk:
                        break
                    activity.progress(request_id, len(chunk), protocol.feed(chunk))
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        reason, transport_end = "client_disconnected", "client_disconnect"
                        break
                    if protocol.complete or protocol.failed:
                        transport_end = "protocol_terminal"
                        break
        finally:
            response.close()
            protocol.finish()
            if protocol.json and protocol.complete:
                activity.progress(request_id, 0, effective=True)
            if reason is None:
                if protocol.failed:
                    reason = "upstream_protocol_error"
                elif protocol.sse and not protocol.complete:
                    reason = "upstream_incomplete_stream"
            activity.finish(request_id, reason, protocol.complete if protocol.known else None,
                            transport_end)
            self.close_connection = True


def _load_adapter(name):
    if not name:
        return None
    try:
        module = importlib.import_module(name)
    except ImportError as error:
        raise GatewayError("cannot import adapter %s" % name) from error
    adapt = getattr(module, "adapt", None)
    if not callable(adapt):
        raise GatewayError("adapter %s has no adapt function" % name)
    return adapt


def run(command, upstream, port, timeout_seconds, trace_path, summary_path, adapter_name,
        activity_path=None, monitor_options=None):
    if not command or not all(isinstance(item, str) and item and "\x00" not in item for item in command):
        raise GatewayError("agent command must be a non-empty argv")
    if isinstance(port, bool) or not isinstance(port, int) or port <= 0 or port > 65535:
        raise GatewayError("gateway port must be between 1 and 65535")
    if timeout_seconds <= 0:
        raise GatewayError("upstream timeout must be positive")
    upstream = normalize_upstream(upstream)
    adapter = _load_adapter(adapter_name)
    trace = _Trace(trace_path)
    options = dict(MONITOR_DEFAULTS)
    options.update(monitor_options or {})
    check_model_monitor(options)
    server = _Server(("127.0.0.1", port), upstream, timeout_seconds, trace, adapter)
    activity = GatewayActivity(activity_path, options, trace)
    server.activity = activity
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    child = None
    code = None
    previous = []
    try:
        thread.start()
        child = subprocess.Popen(list(command), env=os.environ.copy())

        def forward(signum, _frame):
            if child is not None and child.poll() is None:
                child.send_signal(signum)

        if threading.current_thread() is threading.main_thread():
            for signum in (signal.SIGINT, signal.SIGTERM):
                previous.append((signum, signal.getsignal(signum)))
                signal.signal(signum, forward)
        code = child.wait()
    finally:
        for signum, handler in previous:
            signal.signal(signum, handler)
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                code = child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                code = child.wait()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        activity.close()
        summary = trace.summary()
        trace.close()
        if summary_path:
            path = Path(summary_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".tmp")
            temporary.write_text(json.dumps(summary, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(str(temporary), str(path))
    if code is None:
        raise GatewayError("agent command did not return an exit code")
    return code


def main(argv=None):
    parser = argparse.ArgumentParser(description="Forward model calls and record metadata only")
    parser.add_argument("--upstream-base-url", required=True)
    parser.add_argument("--port", type=int, default=LOCAL_PORT)
    parser.add_argument("--upstream-timeout", type=float, default=21600.0)
    parser.add_argument("--trace-path", required=True)
    parser.add_argument("--summary-path")
    parser.add_argument("--adapter", default="")
    parser.add_argument("--activity-path")
    parser.add_argument("--monitor-options", help="JSON operational limits, no credentials")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    try:
        return run(command, args.upstream_base_url, args.port, args.upstream_timeout,
                   args.trace_path, args.summary_path, args.adapter, args.activity_path,
                   json.loads(args.monitor_options) if args.monitor_options else None)
    except (GatewayError, OSError) as error:
        print("error: model gateway failed: %s" % error, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
