"""Stage 1 acceptance: the controller depends only on the container-service interface."""
import io
import json
import os
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from science_innovation_exam import action_client, container_service
from science_innovation_exam.actions import Actions
from science_innovation_exam.agents import prepare_agent
from science_innovation_exam.common.errors import ContractError
from science_innovation_exam.config import load_config
from science_innovation_exam.container_service.channel_mailbox import MailboxChannel
from science_innovation_exam.container_service.docker import DockerService
from science_innovation_exam.container_service.interface import (Channel, ContainerService, Handle, ImageRef,
                                                                  MountSpec)
from science_innovation_exam.runner import run
from tests.helpers import _make_package, _write_json


def envelope(action, payload, key=None):
    return {"schema_version": "1.0", "request_id": key or str(uuid.uuid4()), "action": action, "payload": payload}


class FakeHandle(Handle):
    def __init__(self, log):
        self.log = log

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0

    def stop(self):
        self.log.append("stop")


class FakeChannel(Channel):
    def __init__(self, log, handler):
        self.log, self.handler = log, handler
        self.mounts = [MountSpec("control", "/control", False)]
        self.environment = {"SIE_ACTION_TRANSPORT": "mailbox", "SIE_MAILBOX": "/control"}

    def close(self):
        self.log.append("close")


class FakeContainerService(ContainerService):
    """Plays both the backend and the agent: every request goes through the channel handler."""
    def __init__(self, config, run_dir):
        super().__init__(config, run_dir)
        self.log = []
        self.mounts_seen = []
        (self.run_dir / "control").mkdir()

    def check(self):
        self.log.append("check")

    def publish(self, image_id, local_tag):
        self.log.append("publish")
        return ImageRef("fake://" + local_tag, image_id)

    def open_channel(self, handler):
        self.log.append("open_channel")
        self.channel = FakeChannel(self.log, handler)
        return self.channel

    def _host(self, spec, target):
        self.mounts_seen.extend(spec.mounts)
        return next(m.resolve(self.run_dir) for m in spec.mounts if m.target == target)

    def start_agent(self, spec):
        self.log.append("start_agent")
        assert spec.image == "fake://sie-cpu:local-tag" and spec.network_mode == "proxy", spec
        assert not {"/verifier", "/verify-context"} & {m.target for m in spec.mounts}, "agent sees private mounts"
        assert spec.workdir == "/tmp"
        assert "/workspace" not in {mount.target for mount in spec.mounts}
        submission = self._host(spec, "/submission")
        (submission / "submission.json").write_text('{"value": 7}')
        (submission / "gate.py.orig").write_text("backup")
        (submission / "__pycache__").mkdir()
        (submission / "__pycache__/gate.pyc").write_bytes(b"cache")
        reply = self.channel.handler(json.dumps(envelope("validation", {"value": 3})).encode())
        assert reply["ok"] and reply["response"] == {"count": 1}, reply
        reply = self.channel.handler(json.dumps(envelope("test", None)).encode())
        assert reply["ok"] and reply["terminal"], reply
        return FakeHandle(self.log)

    def run_verifier(self, spec):
        self.log.append("run_verifier:" + spec.role)
        assert spec.network_mode == "offline"
        output = self._host(spec, "/output")
        if spec.role == "validation":
            _write_json(output / "response.json", {"count": 1})
        else:
            submission = self._host(spec, "/submission")
            assert submission.name == "frozen-submission"
            assert sorted(p.name for p in submission.iterdir()) == ["submission.json"]
            _write_json(output / "result.json", {
                "schema_version": "1.0", "task_id": "integer_echo", "valid": True, "status": "valid",
                "primary_metric": {"name": "accuracy", "value": 1.0, "direction": "maximize"},
                "metrics": [{"name": "accuracy", "value": 1.0, "unit": None}], "error": None})
        return 0

    def cleanup(self):
        self.log.append("cleanup")


class ContainerServiceTests(unittest.TestCase):
    def config(self, root, **extra):
        task = root / "integer_echo"
        if not task.exists():
            _make_package(root)
        value = {"task": str(task), "output": str(root / "runs"),
                 "agent": {"type": "scripted", "command": ["python3", "-c", "pass"]}}
        value.update(extra)
        _write_json(root / "config.yaml", value)
        return load_config(root / "config.yaml")

    def test_load_selects_docker_or_a_named_plugin(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = self.config(root)
            self.assertIsInstance(container_service.load(config, root), DockerService)
            with self.assertRaisesRegex(ContractError, "module is required"):
                self.config(root, container_service={"type": "x"})
            with self.assertRaisesRegex(ContractError, "unknown container_service fields"):
                self.config(root, container_service={"type": "docker", "registry": "r"})
            with self.assertRaisesRegex(ContractError, "socket or mailbox"):
                self.config(root, container_service={"type": "docker", "action_transport": "pigeon"})
            config = self.config(root, container_service={"type": "x", "module": "no_such_plugin_module"})
            with self.assertRaisesRegex(ContractError, "cannot be imported"):
                container_service.load(config, root)
            config = self.config(root, container_service={"type": "x", "module": "json"})
            with self.assertRaisesRegex(ContractError, "must define create"):
                container_service.load(config, root)

    def test_runner_uses_only_the_interface_in_a_fixed_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.config(root)
            services = []

            def load(config, run_dir):
                services.append(FakeContainerService(config, run_dir))
                return services[-1]
            result = run(root / "config.yaml", load_service=load,
                         prepare_image=lambda task, config, run_dir, build_args=None: ("sha256:fake", "sie-cpu:local-tag"))
            self.assertEqual(result["status"], "scored", result)
            self.assertEqual(result["image"], "sha256:fake")
            self.assertEqual(result["image_uri"], "fake://sie-cpu:local-tag")
            self.assertEqual(result["container_service"], "docker")
            service = services[0]
            self.assertEqual(service.log, ["check", "publish", "open_channel", "start_agent", "run_verifier:validation",
                                           "stop", "run_verifier:test", "close", "cleanup"])
            run_dir = Path(result["run_directory"])
            self.assertTrue(service.mounts_seen)
            for mount in service.mounts_seen:
                if mount.shared:
                    self.assertFalse(mount.is_relative(), mount)
                    self.assertTrue(Path(mount.source).resolve().exists(), mount)
                    self.assertNotIn(run_dir.resolve(), Path(mount.source).resolve().parents, mount)
                else:
                    self.assertTrue(mount.is_relative(), mount)
                    self.assertTrue(mount.resolve(run_dir).exists(), mount)
                    self.assertIn(run_dir.resolve(), mount.resolve(run_dir).parents, mount)
            self.assertFalse((run_dir / "task").exists())
            self.assertFalse((run_dir / "workspace").exists())
            record = json.loads((run_dir / "task-package.json").read_text())
            self.assertEqual(record["task_id"], "integer_echo")
            self.assertEqual(len(record["fingerprint"]), 64)
            self.assertTrue((run_dir / "actions").is_dir())
            self.assertTrue((run_dir / "submission/submission.json").is_file())

    def test_mailbox_round_trip_replay_rejection_and_half_written_files(self):
        class Validator:
            calls = 0

            def invoke(self, kind, directory, request=None, submission=None):
                self.calls += 1
                return {"round": self.calls, "echo": request}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            actions = Actions(Validator(), root / "actions", {"request_bytes": 4096})
            channel = MailboxChannel(root / "control", actions.serve_bytes, poll_seconds=0.05)
            self.assertEqual(channel.environment["SIE_ACTION_TRANSPORT"], "mailbox")
            self.assertEqual(channel.mounts, [MountSpec("control", "/control", False)])
            env = {"SIE_ACTION_TRANSPORT": "mailbox", "SIE_MAILBOX": str(root / "control"),
                   "SIE_ACTION_TIMEOUT": "10", "SIE_MAILBOX_POLL_SECONDS": "0.05"}
            try:
                with patch.dict(os.environ, env):
                    # A half-written request is never read.
                    (root / "control/requests/half.json.tmp").write_text("{")
                    self.assertFalse(channel.serve_once())
                    # Normal round trip through the real agent-side CLI.
                    request_file = root / "request.json"
                    _write_json(request_file, {"value": 3})
                    key = str(uuid.uuid4())
                    output = io.StringIO()
                    with patch("sys.argv", ["sie-action", "validation", "--request", str(request_file), "--request-id", key]), \
                            patch("sys.stdout", output):
                        self.assertEqual(action_client.main(), 0)
                    first = json.loads(output.getvalue())
                    self.assertEqual(first["response"], {"round": 1, "echo": {"value": 3}})
                    # Same id and content: the stored reply, no second validation.
                    again = action_client.via_mailbox(envelope("validation", {"value": 3}, key), 10, 10 ** 6)
                    self.assertEqual(again, first)
                    self.assertEqual(actions.verifier.calls, 1)
                    # Same id, different content: rejected once the controller actually reads it.
                    # (While the reply file for that id exists the agent just re-reads the stored reply.)
                    reject = action_client.via_mailbox(envelope("validation", {"value": 4}, key), 10, 10 ** 6)
                    self.assertEqual(reject, first)
                    (root / "control/responses" / (key + ".json")).unlink()
                    reject = action_client.via_mailbox(envelope("validation", {"value": 4}, key), 10, 10 ** 6)
                    self.assertFalse(reject["ok"])
                    self.assertIn("reused", reject["error"])
                    # Concurrent agent thread and terminal action.
                    results = []
                    thread = threading.Thread(target=lambda: results.append(action_client.via_mailbox(envelope("test", None), 10, 10 ** 6)))
                    thread.start()
                    thread.join(5)
                    self.assertTrue(results and results[0]["terminal"], results)
                    self.assertTrue(actions.done.is_set())
                    self.assertTrue((root / "control/requests/half.json.tmp").exists())
                    self.assertEqual(len(list((root / "control/requests").glob("*.json"))), 0)
            finally:
                channel.close()

    def test_scripted_agent_is_staged_into_the_run_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            script = root / "outside.py"
            script.write_text("print('hi')\n")
            config = self.config(root, agent={"type": "scripted", "script": str(script), "command": ["python3", "/agent.py"]})
            run_dir = root / "run"
            run_dir.mkdir()
            launch = prepare_agent(config, run_dir, {"SIE_ACTION_TRANSPORT": "socket"})
            self.assertEqual((run_dir / "agent/agent.py").read_text(), "print('hi')\n")
            self.assertEqual([(m.source, m.target) for m in launch.mounts], [("agent-state", "/state"), ("agent/agent.py", "/agent.py")])
            self.assertEqual(launch.environment["SIE_ACTION_TRANSPORT"], "socket")
            self.assertEqual(launch.secrets, [])
            self.assertEqual(launch.log_paths, [])
