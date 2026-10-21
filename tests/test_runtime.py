import io
import json
import os
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from science_innovation_exam.actions import Actions
from science_innovation_exam.agents import archive_logs_command, prepare_agent, prompt_for
from science_innovation_exam.container_service.channel_socket import _Handler
from science_innovation_exam.common.errors import ContractError, InfrastructureError, ScoringError
from science_innovation_exam.config import load_config
from science_innovation_exam.common.host import require_disk
from science_innovation_exam.task import TaskPackage
from science_innovation_exam.verifier import Verifier, validate_result
from science_innovation_exam.verify_context import VerifyContext
from tests.helpers import _make_package, _write_json


def request(action="validation", payload=None):
    return {"schema_version": "1.0", "request_id": "11111111-1111-4111-8111-111111111111",
            "action": action, "payload": {} if action == "validation" and payload is None else payload}


class RuntimeTests(unittest.TestCase):
    def test_same_id_replays_feedback_and_rejects_changed_payload(self):
        class Validator:
            calls = 0
            def invoke(self, *args, **kwargs):
                self.calls += 1
                return {"round": self.calls}
        with tempfile.TemporaryDirectory() as temporary:
            validator = Validator()
            actions = Actions(validator, Path(temporary) / "actions", {})
            first = actions.handle(request(payload={"selection": [1]}))
            self.assertEqual(first, actions.handle(request(payload={"selection": [1]})))
            self.assertEqual(validator.calls, 1)
            with self.assertRaises(ContractError):
                actions.handle(request(payload={"selection": [2]}))
            self.assertEqual(len(list(actions.root.glob("*.json"))), 1)

    def test_terminal_disconnect_still_notifies_controller(self):
        class Disconnect:
            def write(self, data):
                raise BrokenPipeError()
        class Connection:
            def settimeout(self, seconds):
                pass
        with tempfile.TemporaryDirectory() as temporary:
            actions = Actions(None, Path(temporary) / "actions", {"request_bytes": 4096})
            handler = object.__new__(_Handler)
            handler.server = type("Server", (), {"handler": actions.serve_bytes, "maximum": 4096})()
            handler.connection = Connection()
            handler.rfile = io.BytesIO((json.dumps(request("test")) + "\n").encode())
            handler.wfile = Disconnect()
            handler.handle()
            self.assertTrue(actions.done.is_set())
            self.assertTrue(actions.test_requested)

    def test_validation_failure_is_persisted_without_exposing_detail(self):
        class Broken:
            def invoke(self, *args, **kwargs):
                raise ScoringError("private failure detail")
        with tempfile.TemporaryDirectory() as temporary:
            actions = Actions(Broken(), Path(temporary) / "actions", {})
            response = actions.handle(request())
            self.assertEqual(actions.failure["status"], "validation_failed")
            self.assertNotIn("private failure detail", json.dumps(response))

    def test_missing_and_escaping_submissions_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = TaskPackage.load(_make_package(root))
            final = root / "final"
            final.mkdir()
            with self.assertRaisesRegex(ContractError, "missing submission"):
                task.freeze(final, root / "frozen")
            (final / "submission.json").symlink_to(task.input_root / "value.json")
            with self.assertRaisesRegex(ContractError, "links"):
                task.freeze(final, root / "frozen")
            (final / "submission.json").unlink()
            os.mkfifo(final / "submission.json")
            with self.assertRaisesRegex(ContractError, "regular file"):
                task.freeze(final, root / "frozen")

    def test_freeze_collects_only_declared_files_and_preserves_source(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = TaskPackage.load(_make_package(root))
            final = root / "final"
            _write_json(final / "submission.json", {"value": 7})
            (final / "__pycache__").mkdir()
            (final / "__pycache__/gate.pyc").write_bytes(b"cache")
            (final / "gate.py.orig").write_text("backup")
            (final / "unrelated-link").symlink_to(root / "missing")
            frozen = root / "frozen"
            task.freeze(final, frozen)
            self.assertEqual(["submission.json"], sorted(p.name for p in frozen.iterdir()))
            self.assertEqual((final / "submission.json").read_bytes(), (frozen / "submission.json").read_bytes())
            self.assertEqual((frozen / "submission.json").stat().st_mode & 0o222, 0)
            _write_json(final / "submission.json", {"value": 8})
            self.assertEqual(json.loads((frozen / "submission.json").read_text())["value"], 7)
            self.assertEqual((final / "gate.py.orig").read_text(), "backup")
            with self.assertRaisesRegex(ContractError, "separate directory"):
                task.freeze(final, final)

    def test_baseline_separates_a_measured_method_from_an_expert_score(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package_root = _make_package(root)
            meta_path = package_root / "meta.json"
            meta = json.loads(meta_path.read_text())

            expert = dict(
                meta,
                baseline={
                    "existing_baseline": False,
                    "name": "",
                    "source": "",
                    "score": 0.9,
                    "bottom_line_score": 0.1,
                },
            )
            _write_json(meta_path, expert)
            loaded = TaskPackage.load(package_root).meta["baseline"]
            self.assertEqual((0.9, 0.1), (loaded["score"], loaded["bottom_line_score"]))

            without = {key: value for key, value in meta.items() if key != "baseline"}
            _write_json(meta_path, without)
            self.assertNotIn("baseline", TaskPackage.load(package_root).meta)

            for broken in (
                {"existing_baseline": True, "name": "", "source": "https://x.invalid/b", "score": 1.0, "bottom_line_score": 0.0},
                {"existing_baseline": False, "name": "Guess", "source": "", "score": 1.0, "bottom_line_score": 0.0},
                {"name": "Copy", "source": "https://x.invalid/b", "score": 1.0, "bottom_line_score": 0.0},
                {"existing_baseline": True, "name": "Copy", "url": "https://x.invalid/b", "score": 1.0, "bottom_line_score": 0.0},
                {"existing_baseline": True, "name": "Copy", "source": "https://x.invalid/b", "score": 1.0},
            ):
                _write_json(meta_path, dict(meta, baseline=broken))
                with self.assertRaises(ContractError):
                    TaskPackage.load(package_root)

    def test_reference_anchor_must_beat_the_bottom_line_in_the_metric_direction(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package_root = _make_package(root)
            meta_path = package_root / "meta.json"
            meta = json.loads(meta_path.read_text())
            transform = {
                "raw_direction": "minimize",
                "parameters": {},
                "anchors": {
                    "lower": {"raw_value": "1", "matrix_score": "0"},
                    "reference": {"raw_value": "0", "matrix_score": "1000"},
                },
                "invalid_score": "0",
                "precision": 50,
                "rounding": {"decimal_places": 6, "mode": "half_even"},
            }
            meta["metric"] = {
                "description": "Lower is better.",
                "type": "linear_minimize",
                "transform": transform,
            }

            def baseline(score, bottom):
                return {
                    "existing_baseline": False,
                    "name": "",
                    "source": "",
                    "score": score,
                    "bottom_line_score": bottom,
                }

            _write_json(meta_path, dict(meta, baseline=baseline(-0.35, 1.08)))
            TaskPackage.load(package_root)

            for score, bottom in ((1.08, -0.35), (0.5, 0.5)):
                _write_json(meta_path, dict(meta, baseline=baseline(score, bottom)))
                with self.assertRaisesRegex(ContractError, "strictly better"):
                    TaskPackage.load(package_root)

            # A raw_only metric declares no direction, so the ordering is not checkable here.
            meta["metric"] = {"description": "Lower is better.", "type": "raw_only"}
            _write_json(meta_path, dict(meta, baseline=baseline(1.08, -0.35)))
            TaskPackage.load(package_root)

    def test_linear_metric_may_omit_transform_when_baseline_anchors_exist(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package_root = _make_package(root)
            meta_path = package_root / "meta.json"
            meta = json.loads(meta_path.read_text())
            meta["metric"] = {"description": "Higher is better.", "type": "linear_maximize"}
            _write_json(
                meta_path,
                dict(
                    meta,
                    baseline={
                        "existing_baseline": False,
                        "name": "",
                        "source": "",
                        "score": 20.0,
                        "bottom_line_score": 10.0,
                    },
                ),
            )
            self.assertNotIn("transform", TaskPackage.load(package_root).meta["metric"])
            _write_json(meta_path, {key: value for key, value in meta.items() if key != "baseline"})
            with self.assertRaisesRegex(ContractError, "without transform requires baseline"):
                TaskPackage.load(package_root)

    def test_limitation_rejects_a_storage_quota_the_runner_cannot_enforce(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package_root = _make_package(root)
            instruction_path = package_root / "instruction.json"
            instruction = json.loads(instruction_path.read_text())
            instruction["limitation"] = {"cpu": {"cores": 8}, "storage": {"gb": 20}}
            _write_json(instruction_path, instruction)
            with self.assertRaises(ContractError):
                TaskPackage.load(package_root)

    def test_ram_output_is_rejected(self):
        with self.assertRaises(InfrastructureError):
            require_disk(Path("/tmp/sie-test-output"))

    def test_config_resolves_paths_and_rejects_old_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            value = {"task": "task", "output": "runs", "agent": {"type": "scripted", "command": ["python3", "script.py"]}}
            _write_json(root / "run.yaml", value)
            config = load_config(root / "run.yaml")
            self.assertEqual(config["task"], str(root / "task"))
            self.assertEqual(config["resources"]["cpus"], 2)
            self.assertEqual(config["network"]["mode"], "proxy")
            value["expected_payload_sha256"] = "old-lock"
            _write_json(root / "run.yaml", value)
            with self.assertRaisesRegex(ContractError, "unknown"):
                load_config(root / "run.yaml")

    def test_codex_launch_uses_environment_credentials_and_new_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = TaskPackage.load(_make_package(root))
            bundle = root / "bundle/bin"
            bundle.mkdir(parents=True)
            (bundle / "codex").write_text("#!/bin/sh\nexit 0\n")
            _write_json(root / "config.yaml", {"task": str(task.root), "output": "runs", "network": {"mode": "proxy"},
                        "agent": {"type": "codex", "bundle": str(bundle.parent), "model": "test-model"}})
            config = load_config(root / "config.yaml")
            run_dir = root / "run"
            run_dir.mkdir()
            with patch.dict(os.environ, {"OPENAI_API_KEY": "test-secret", "OPENAI_BASE_URL": "https://model.example/v1"}):
                launch = prepare_agent(config, run_dir, {"SIE_ACTION_TRANSPORT": "socket"})
            self.assertNotIn("test-secret", " ".join(launch.command))
            self.assertEqual(launch.environment["SIE_MODEL_GATEWAY_CREDENTIAL"], "test-secret")
            self.assertEqual(launch.environment["HOME"], "/var/lib/sie-home")
            self.assertIn("science_innovation_exam.model_gateway", launch.command)
            self.assertNotIn("-C", launch.command)
            self.assertIn("/submission", launch.command[-1])
            self.assertIn("sie-action test", launch.command[-1])
            self.assertNotIn("/workspace", launch.command[-1])
            self.assertNotIn(str(config["resources"]), launch.command[-1])
            self.assertNotIn("test-secret", (run_dir / "agent-state/config.toml").read_text())
            self.assertFalse((run_dir / "agent/codex").exists())
            bundle_mount = next(mount for mount in launch.mounts if mount.target == "/opt/codex")
            self.assertTrue(bundle_mount.shared.startswith("agent_bundles/codex/"))
            self.assertEqual(bundle_mount.resolve(run_dir), bundle.parent.resolve())
            for mount in launch.mounts:
                if mount.shared:
                    self.assertTrue(Path(mount.source).is_absolute())
                else:
                    self.assertTrue(mount.is_relative(), mount)
                    self.assertTrue(mount.resolve(run_dir).exists(), mount)
            settings = (run_dir / "agent-state/config.toml").read_text()
            self.assertIn("SIE_ACTION_TRANSPORT = \"socket\"", settings)
            self.assertIn('HOME = "/var/lib/sie-home"', settings)
            # Proxy variables for research shells come from the container service, never from a fixed hostname.
            self.assertNotIn("HTTP_PROXY", settings)
            self.assertIn("/var/lib/sie-codex", " ".join(launch.local_setup))
            self.assertEqual(launch.log_paths, [("/var/lib/sie-home/sessions", "sessions"),
                                                 ("/var/lib/sie-home/shell_snapshots", "shell_snapshots")])
            shutil.rmtree(run_dir)
            run_dir.mkdir()
            with patch.dict(os.environ, {"OPENAI_API_KEY": "test-secret", "OPENAI_BASE_URL": "https://model.example/v1"}):
                prepare_agent(config, run_dir, {}, {"HTTP_PROXY": "http://egress:8080"})
            self.assertIn("HTTP_PROXY = \"http://egress:8080\"", (run_dir / "agent-state/config.toml").read_text())

    def test_failed_verifier_restores_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = TaskPackage.load(_make_package(root))
            context = VerifyContext.create(root / "context", 1000000)
            context["round"] = 1
            class FailingService:
                def run_verifier(self, spec):
                    context["round"] = 2
                    return 1
            config = {"verifier": {}, "limits": {"log_bytes": 10000, "response_bytes": 10000}}
            verifier = Verifier(FailingService(), task, "test", context.root, config, root)
            with patch("science_innovation_exam.verifier.writable"), self.assertRaises(ScoringError):
                verifier.invoke("validation", root / "validation", request={})
            self.assertEqual(context["round"], 1)
            self.assertTrue((root / "validation/error.json").is_file())

    def test_prompt_layers_explain_paths_without_copying_the_task(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            task = TaskPackage.load(_make_package(root))
            _write_json(root / "config.yaml", {"task": str(task.root), "output": "runs",
                        "agent": {"type": "scripted", "command": ["python3", "-c", "pass"]}})
            config = load_config(root / "config.yaml")
            text = prompt_for(config, ["This machine has one example GPU."])
            self.assertIn("Read /instruction.json", text)
            self.assertIn("/submission/<path>", text)
            self.assertIn("payload", text)
            self.assertIn("one example GPU", text)
            self.assertNotIn("/workspace", text)
            self.assertNotIn(task.instruction["instructions"], text)
            self.assertLess(text.index("Read /instruction.json"), text.index("one example GPU"))
            note = root / "agent-note.json"
            note.write_text(json.dumps([{"content": "Use the example agent flag."}]))
            config["agent"]["prompt"] = str(note)
            layered = prompt_for(config, ["This machine has one example GPU."])
            self.assertLess(layered.index("one example GPU"), layered.index("example agent flag"))
            _write_json(root / "old.yaml", {"task": "task", "output": "runs", "system_prompt": "prompt.json",
                        "agent": {"type": "scripted", "command": ["true"]}})
            with self.assertRaisesRegex(ContractError, "unknown config fields"):
                load_config(root / "old.yaml")
            _write_json(root / "relay.yaml", {"task": "task", "output": "runs",
                        "agent": {"type": "scripted", "command": ["true"], "relay": "gateway.py"}})
            with self.assertRaisesRegex(ContractError, "unknown agent fields"):
                load_config(root / "relay.yaml")

    def test_submission_directory_reuses_template_paths_and_fingerprint_is_stable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = _make_package(root)
            instruction = json.loads((package / "instruction.json").read_text())
            instruction["submission"] = [{"path": "results/answer.json", "format": "json",
                                         "description": "answer", "has_template": True}]
            instruction["input"].append({"path": "results/answer.json", "format": "json", "description": "template"})
            _write_json(package / "instruction.json", instruction)
            _write_json(package / "input/results/answer.json", {"value": 1})
            task = TaskPackage.load(package)
            first = task.fingerprint()
            self.assertEqual(first, task.fingerprint())
            submission = root / "submission"
            task.prepare_submission(submission)
            self.assertEqual(json.loads((submission / "results/answer.json").read_text())["value"], 1)
            _write_json(submission / "results/answer.json", {"value": 2})
            frozen = root / "frozen"
            task.freeze(submission, frozen)
            self.assertEqual(json.loads((frozen / "results/answer.json").read_text())["value"], 2)
            self.assertEqual((frozen / "results/answer.json").stat().st_mode & 0o222, 0)
            shutil.rmtree(submission / "results")
            (submission / "results").symlink_to(package / "input/results", target_is_directory=True)
            with self.assertRaisesRegex(ContractError, "links"):
                task.freeze(submission, root / "escaped")
            (package / "input/value.json").write_text('{"value": 8}\n')
            self.assertNotEqual(first, task.fingerprint(refresh=True))
            command = archive_logs_command([("/var/lib/sie-home/sessions", "sessions")])
            self.assertIn("cp -a /var/lib/sie-home/sessions/. /state/sessions/", command)
            self.assertNotIn("/workspace", command)
            with self.assertRaises(ContractError):
                archive_logs_command([("/tmp/x", "../outside")])

    def test_gateway_records_metadata_and_adapter_patches_shape_only(self):
        import socket
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        from science_innovation_exam.codex_adapter import FALLBACK_NAMESPACE_DESCRIPTION
        from science_innovation_exam.model_gateway import main as gateway_main

        class Handler(BaseHTTPRequestHandler):
            body = b""

            def do_POST(self):
                Handler.body = self.rfile.read(int(self.headers["Content-Length"]))
                payload = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args):
                return

        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        upstream_port = probe.getsockname()[1]
        probe.close()
        server = ThreadingHTTPServer(("127.0.0.1", upstream_port), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        gate = socket.socket()
        gate.bind(("127.0.0.1", 0))
        gate_port = gate.getsockname()[1]
        gate.close()
        body = json.dumps({
            "model": "mock-model",
            "stream": True,
            "input": [{"content": "SECRET_PROMPT_TEXT",
                       "tools": [{"type": "namespace", "description": "  ", "name": "x"}]}],
        })
        try:
            with tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "client.py").write_text(
                    "import sys, urllib.request\n"
                    "request = urllib.request.Request(sys.argv[1], data=sys.argv[2].encode(), "
                    "headers={'Content-Type': 'application/json'})\n"
                    "urllib.request.urlopen(request, timeout=10).read()\n")
                with patch.dict(os.environ, {"NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost"}):
                    code = gateway_main([
                        "--upstream-base-url", "http://127.0.0.1:%d/v1" % upstream_port,
                        "--port", str(gate_port), "--upstream-timeout", "10",
                        "--trace-path", str(root / "model-calls.jsonl"),
                        "--summary-path", str(root / "summary.json"),
                        "--activity-path", str(root / "activity.json"),
                        "--adapter", "science_innovation_exam.codex_adapter",
                        "--", "python3", str(root / "client.py"),
                        "http://127.0.0.1:%d/v1/responses" % gate_port, body])
                self.assertEqual(code, 0)
                forwarded = json.loads(Handler.body.decode())
                self.assertEqual(forwarded["input"][0]["tools"][0]["description"], FALLBACK_NAMESPACE_DESCRIPTION)
                self.assertEqual(forwarded["input"][0]["content"], "SECRET_PROMPT_TEXT")
                trace = (root / "model-calls.jsonl").read_text()
                self.assertNotIn("SECRET_PROMPT_TEXT", trace)
                self.assertIn("mock-model", trace)
                self.assertIn('"stream":true', trace)
                summary = json.loads((root / "summary.json").read_text())
                self.assertEqual(summary["content_policy"], "metadata-only")
                self.assertEqual(summary["http_status_counts"]["200"], 1)
                activity = json.loads((root / "activity.json").read_text())
                self.assertFalse(activity['active'])
                self.assertEqual(len(activity['finished']), 1)
        finally:
            server.shutdown()
            server.server_close()
