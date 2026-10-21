"""Generic abort: protocol, real transports, controller shutdown and diagnostics."""
import io
import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from science_innovation_exam import action_client
from science_innovation_exam.actions import Actions
from science_innovation_exam.agents import prompt_for
from science_innovation_exam.container_service.channel_mailbox import MailboxChannel
from science_innovation_exam.container_service.channel_socket import SocketChannel
from science_innovation_exam.runner import run
from tests import test_container_service as services
from tests.helpers import _make_package, _write_json


REASON = '实验设置问题：required input "measurements.csv" is unavailable'


class AbortTests(unittest.TestCase):
    def test_abort_is_terminal_persisted_and_replayable(self):
        with tempfile.TemporaryDirectory() as directory:
            actions = Actions(None, Path(directory) / "actions", {"request_bytes": 4096})
            request = services.envelope("abort", {"reason": REASON})
            response = actions.serve_bytes(json.dumps(request).encode())
            self.assertTrue(response["ok"] and response["terminal"] and response["accepted"])
            self.assertTrue(actions.done.is_set())
            self.assertFalse(actions.test_requested)
            self.assertEqual(actions.abort_reason, REASON)
            self.assertEqual(actions.serve_bytes(json.dumps(request).encode()), response)
            record = json.loads((actions.root / (request["request_id"] + ".json")).read_text())
            self.assertEqual(record, {"request": request, "response": response})
            changed = dict(request, payload={"reason": "different explanation"})
            self.assertIn("reused", actions.serve_bytes(json.dumps(changed).encode())["error"])
            for action, payload in (("test", None), ("validation", {}), ("abort", {"reason": "again"})):
                reply = actions.serve_bytes(json.dumps(services.envelope(action, payload)).encode())
                self.assertFalse(reply["ok"])
                self.assertIn("ended", reply["error"])

    def test_invalid_abort_does_not_end_experiment(self):
        with tempfile.TemporaryDirectory() as directory:
            actions = Actions(None, Path(directory) / "actions", {"request_bytes": 4096})
            for payload in (None, {}, "message", {"reason": 42}, {"reason": "  \n"},
                            {"reason": "message", "type": "predefined"}):
                with self.subTest(payload=payload):
                    reply = actions.serve_bytes(json.dumps(services.envelope("abort", payload)).encode())
                    self.assertFalse(reply["ok"])
                    self.assertFalse(actions.done.is_set())
                    self.assertFalse(actions.closed)
            actions.serve_bytes(json.dumps(services.envelope("test", None)).encode())
            reply = actions.serve_bytes(json.dumps(services.envelope("abort", {"reason": REASON})).encode())
            self.assertFalse(reply["ok"])
            self.assertIsNone(actions.abort_reason)

    def test_cli_abort_through_socket_and_mailbox(self):
        for transport in ("socket", "mailbox"):
            with self.subTest(transport=transport), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                actions = Actions(None, root / "actions", {"request_bytes": 4096})
                channel = (SocketChannel(actions.serve_bytes) if transport == "socket" else
                           MailboxChannel(root / "control", actions.serve_bytes, poll_seconds=0.01))
                environment = dict(channel.environment, SIE_ACTION_TIMEOUT="5", SIE_MAILBOX_POLL_SECONDS="0.01")
                if transport == "socket":
                    environment["SIE_VERIFY_SOCKET"] = str(channel.path)
                else:
                    environment["SIE_MAILBOX"] = str(channel.root)
                try:
                    key = str(uuid.uuid4())
                    argv = ["sie-action", "abort", "--reason", REASON, "--request-id", key]
                    for _ in range(2):
                        output = io.StringIO()
                        with patch.dict(os.environ, environment), patch("sys.argv", argv), patch("sys.stdout", output):
                            self.assertEqual(action_client.main(), 0)
                        self.assertTrue(json.loads(output.getvalue())["terminal"])
                    self.assertEqual(actions.abort_reason, REASON)
                finally:
                    channel.close()

    def test_cli_rejects_missing_blank_and_misplaced_reason(self):
        for args in (("abort",), ("abort", "--reason", "  "),
                     ("abort", "--reason", "issue", "--request", "file"),
                     ("test", "--reason", "issue"), ("validation", "--reason", "issue")):
            with self.subTest(args=args), patch("sys.argv", ["sie-action"] + list(args)), \
                    patch("sys.stderr", io.StringIO()), patch.object(action_client, "via_socket") as send:
                with self.assertRaises(SystemExit) as error:
                    action_client.main()
                self.assertEqual(error.exception.code, 2)
                send.assert_not_called()

    def test_runner_aborts_without_scoring_and_preserves_reason_on_shutdown_failure(self):
        for failure in (None, "stop", "cleanup"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                task = _make_package(root)
                _write_json(root / "config.yaml", {"task": str(task), "output": str(root / "runs"),
                            "agent": {"type": "scripted", "command": ["python3", "-c", "pass"]}})
                class AgentHandle(services.FakeHandle):
                    def stop(self):
                        super().stop()
                        if failure == "stop":
                            raise OSError("agent stop failed")

                class AbortingService(services.FakeContainerService):
                    def start_agent(self, spec):
                        self.log.append("start_agent")
                        reply = self.channel.handler(json.dumps(services.envelope("abort", {"reason": REASON})).encode())
                        assert reply["ok"] and reply["terminal"], reply
                        return AgentHandle(self.log)

                    def cleanup(self):
                        super().cleanup()
                        if failure == "cleanup":
                            raise OSError("resource deletion failed")

                backends = []
                def load(config, run_dir):
                    backends.append(AbortingService(config, run_dir))
                    return backends[-1]
                result = run(root / "config.yaml", load_service=load,
                             prepare_image=lambda *a, **k: ("sha256:fake", "sie-cpu:local-tag"))
                self.assertEqual(result["status"], "infrastructure_failed" if failure else "aborted", result)
                self.assertEqual(result["abort_reason"], REASON)
                self.assertIsNone(result["score"])
                expected = ["check", "publish", "open_channel", "start_agent", "stop"]
                if failure == "stop":
                    expected.append("stop")
                self.assertEqual(backends[0].log, expected + ["close", "cleanup"])
                run_dir = Path(result["run_directory"])
                self.assertFalse((run_dir / "test").exists())
                self.assertFalse((run_dir / "frozen-submission").exists())
                self.assertEqual(json.loads((run_dir / "result.json").read_text())["abort_reason"], REASON)

    def test_abort_guidance_is_in_general_prompt(self):
        prompt = prompt_for({"agent": {}})
        self.assertIn("sie-action abort --reason", prompt)
        self.assertIn("experiment setup", prompt)
        self.assertIn("no predefined reason categories", prompt)
