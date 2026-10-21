"""Agent-side client. This module has no private verifier dependencies.

Two transports, chosen by SIE_ACTION_TRANSPORT:
  socket   (default) one JSON line over the Unix socket SIE_VERIFY_SOCKET
  mailbox  write $SIE_MAILBOX/requests/<id>.json atomically, then poll
           $SIE_MAILBOX/responses/<id>.json every SIE_MAILBOX_POLL_SECONDS
"""
import argparse
import json
import os
import socket
import sys
import time
import uuid


def via_socket(request, timeout, maximum):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(os.environ.get("SIE_VERIFY_SOCKET", "/control/verify.sock"))
        client.sendall((json.dumps(request, allow_nan=False) + "\n").encode())
        with client.makefile("rb") as reader:
            data = reader.readline(maximum + 1)
    if len(data) > maximum:
        raise ValueError("action response is too large")
    return json.loads(data)


def via_mailbox(request, timeout, maximum):
    root = os.environ.get("SIE_MAILBOX", "/control")
    name = request["request_id"] + ".json"
    final = os.path.join(root, "requests", name)
    temporary = final + ".tmp"
    with open(temporary, "w") as handle:
        handle.write(json.dumps(request, allow_nan=False) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.rename(temporary, final)
    responses = os.path.join(root, "responses")
    response = os.path.join(responses, name)
    interval = float(os.environ.get("SIE_MAILBOX_POLL_SECONDS", "0.5"))
    deadline = time.monotonic() + timeout
    # Poll by listing the directory, not by stat: on a network filesystem a negative
    # lookup for a file that does not exist yet can be cached for tens of seconds,
    # while a directory read revalidates within about a second.
    while name not in os.listdir(responses):
        if time.monotonic() >= deadline:
            raise socket.timeout("no response in %s seconds" % timeout)
        time.sleep(interval)
    with open(response, "rb") as handle:
        data = handle.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("action response is too large")
    return json.loads(data)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("validation", "test", "abort"))
    parser.add_argument("--request")
    parser.add_argument("--reason", help="brief free-text explanation for abort")
    parser.add_argument("--request-id", default=None, help="reuse this ID to retry the same request")
    args = parser.parse_args()
    if args.action == "abort":
        if not args.reason or not args.reason.strip():
            parser.error("abort requires a non-empty --reason")
        if args.request:
            parser.error("abort does not accept --request")
    elif args.reason is not None:
        parser.error("--reason is only accepted by abort")
    key = args.request_id or str(uuid.uuid4())
    try:
        if args.action == "validation":
            if not args.request:
                parser.error("validation requires --request")
            with open(args.request) as handle:
                payload = json.load(handle)
        elif args.action == "abort":
            payload = {"reason": args.reason}
        else:
            if args.request:
                parser.error("test does not accept --request")
            payload = None
        request = {"schema_version": "1.0", "request_id": key, "action": args.action, "payload": payload}
        timeout = float(os.environ.get("SIE_ACTION_TIMEOUT", "150"))
        maximum = int(os.environ.get("SIE_ACTION_RESPONSE_BYTES", "9000000"))
        transport = os.environ.get("SIE_ACTION_TRANSPORT", "socket")
        if transport not in ("socket", "mailbox"):
            raise ValueError("unknown SIE_ACTION_TRANSPORT %r" % transport)
        response = (via_mailbox if transport == "mailbox" else via_socket)(request, timeout, maximum)
        print(json.dumps(response))
        return 0 if response.get("ok") else 1
    except (OSError, ValueError) as error:
        print("sie-action request_id=%s: %s" % (key, error), file=sys.stderr)
        return 125


if __name__ == "__main__":
    raise SystemExit(main())
