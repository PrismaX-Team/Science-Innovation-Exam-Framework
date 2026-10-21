"""HTTP/CONNECT proxy with domain rules and bounded metadata logs, no billing."""
import argparse
import ipaddress
import json
import select
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


class ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _target(self, host, port):
        policy = self.server.policy
        if not host or port not in policy.get("ports", [80, 443]):
            raise ValueError("port is not allowed")
        host = host.lower().rstrip(".")
        for domain in policy["denied_domains"]:
            domain = domain.lower()
            if domain.startswith("*."):
                domain = domain[2:]
            if host == domain or host.endswith("." + domain):
                raise ValueError("domain is denied")
        addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        if host not in policy.get("allowed_private_hosts", []):
            if any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
                raise ValueError("private network destination is denied")
        return addresses[0][4][0]

    def _log(self, host, port, status):
        record = (json.dumps({"method": self.command, "host": host, "port": port, "status": status}) + "\n").encode()
        with self.server.log_lock:
            path = self.server.log_path
            if (path.stat().st_size if path.exists() else 0) + len(record) <= self.server.maximum_log_bytes:
                with path.open("ab") as out:
                    out.write(record)

    def _connect(self, host, port, address):
        upstream = self.server.policy.get("upstream_proxy")
        if not upstream:
            return socket.create_connection((address, port), timeout=15)
        proxy = urlsplit(upstream)
        if proxy.scheme != "http" or not proxy.hostname or proxy.username or proxy.password:
            raise ValueError("upstream proxy must be an HTTP endpoint without credentials")
        connection = socket.create_connection((proxy.hostname, proxy.port or 80), timeout=15)
        # Pin the resolved target address even when the cluster supplies transport.
        authority = "[%s]:%d" % (address, port) if ":" in address else "%s:%d" % (address, port)
        connection.sendall(("CONNECT %s HTTP/1.1\r\nHost: %s\r\n\r\n" % (authority, authority)).encode())
        response = b""
        while not response.endswith(b"\r\n\r\n") and len(response) < 16384:
            block = connection.recv(1)
            if not block:
                break
            response += block
        if b" 200 " not in response.split(b"\r\n", 1)[0]:
            connection.close()
            raise OSError("upstream proxy rejected CONNECT")
        return connection

    def _tunnel(self, remote):
        sockets = [self.connection, remote]
        while True:
            ready, _, _ = select.select(sockets, [], [], 120)
            if not ready:
                return
            for source in ready:
                data = source.recv(65536)
                if not data:
                    return
                (remote if source is self.connection else self.connection).sendall(data)

    def do_CONNECT(self):
        parsed = urlsplit("//" + self.path)
        self._forward(parsed.hostname, parsed.port or 443, tunnel=True)

    def do_GET(self):
        parsed = urlsplit(self.path)
        self._forward(parsed.hostname, parsed.port or 80, tunnel=False, parsed=parsed)

    do_POST = do_GET
    do_HEAD = do_GET
    do_PUT = do_GET
    do_DELETE = do_GET

    def _forward(self, host, port, tunnel, parsed=None):
        started = False
        try:
            address = self._target(host, port)
            with self._connect(host, port, address) as remote:
                if tunnel:
                    self.send_response(200, "Connection established")
                    self.end_headers()
                    started = True
                    self._log(host, port, "allowed")
                    self._tunnel(remote)
                else:
                    if parsed.scheme != "http" or self.headers.get("Transfer-Encoding"):
                        raise ValueError("use HTTP with Content-Length or HTTPS CONNECT")
                    path = (parsed.path or "/") + ("?" + parsed.query if parsed.query else "")
                    headers = ["%s %s HTTP/1.1" % (self.command, path)]
                    headers += ["%s: %s" % (key, value) for key, value in self.headers.items()
                                if key.lower() not in {"connection", "proxy-connection", "proxy-authorization"}]
                    headers.append("Connection: close")
                    remote.sendall(("\r\n".join(headers) + "\r\n\r\n").encode())
                    remaining = int(self.headers.get("Content-Length", 0))
                    while remaining:
                        block = self.rfile.read(min(65536, remaining))
                        if not block:
                            raise OSError("incomplete HTTP body")
                        remote.sendall(block)
                        remaining -= len(block)
                    started = True
                    self._log(host, port, "allowed")
                    while True:
                        data = remote.recv(65536)
                        if not data:
                            break
                        self.wfile.write(data)
        except (OSError, ValueError) as error:
            self._log(host, port, str(error))
            if not started:
                self.send_error(403, "Proxy request rejected")
        finally:
            self.close_connection = True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--policy", required=True)
    parser.add_argument("--state", required=True)
    parser.add_argument("--maximum-log-bytes", type=int, required=True)
    args = parser.parse_args()
    state = Path(args.state)
    server = ThreadingHTTPServer(("0.0.0.0", 8080), ProxyHandler)
    server.policy = json.loads(Path(args.policy).read_text())
    server.log_lock = threading.Lock()
    server.log_path = state / "access.jsonl"
    server.maximum_log_bytes = args.maximum_log_bytes
    (state / "ready").write_text("ready\n")
    server.serve_forever()


if __name__ == "__main__":
    main()
