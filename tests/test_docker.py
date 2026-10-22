"""Real-container integration checks, enabled explicitly by tools/test-docker.py."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from science_innovation_exam.config import load_config
from science_innovation_exam.common.errors import InfrastructureError
from science_innovation_exam.common.host import writable
from science_innovation_exam.bounded_output import command as logged_command
from science_innovation_exam.agents import prepare_agent
from science_innovation_exam.container_service.docker import Docker, DockerService
from science_innovation_exam.runner import run
from science_innovation_exam.recovery import cleanup_run
from science_innovation_exam.resources import ResourceJournal
from science_innovation_exam.task import TaskPackage
from science_innovation_exam.verifier import Verifier
from science_innovation_exam.verify_context import VerifyContext
from tests.helpers import _make_package, _write_json


AGENT = '''import json, subprocess, time
from pathlib import Path
for path in ['/verifier', '/verify-context', '/var/run/docker.sock']:
    assert not Path(path).exists(), path
try:
    Path('/input/value.json').write_text('tampered')
except OSError:
    pass
else:
    raise AssertionError('input is writable')
mode = json.loads(Path('/input/value.json').read_text()).get('mode', 'normal')
if mode == 'timeout':
    time.sleep(300)
if mode == 'exit':
    raise SystemExit(7)
if mode == 'exit137':
    raise SystemExit(137)
if mode == 'abort':
    subprocess.run(['sie-action', 'abort', '--reason', 'Required experiment input is unavailable'], check=True)
    time.sleep(300)  # The controller must stop us rather than wait for normal exit.
Path('/tmp/validation.json').write_text(json.dumps({'value': 3}))
reply = subprocess.run(['sie-action', 'validation', '--request', '/tmp/validation.json'], capture_output=True, text=True)
if reply.returncode:
    print(reply.stdout, reply.stderr, flush=True)
    raise SystemExit(reply.returncode)
assert json.loads(reply.stdout)['response']['count'] == 1
if mode != 'missing':
    Path('/submission/submission.json').write_text('{"value": 7}')
if mode == 'directory':
    model = Path('/submission/model')
    assert json.loads((model / 'nested/config.json').read_text()) == {'value': 1}
    assert (model / 'empty').is_dir()
    (model / 'nested/config.json').write_text('{"value": 7}')
    (model / 'nested/.weights').write_bytes(b'weights')
    Path('/submission/unlisted-backup').write_text('backup')
subprocess.run(['sie-action', 'test'])
'''


@unittest.skipUnless(os.environ.get("SIE_DOCKER_TESTS") == "1", "explicit Docker acceptance only")
class DockerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.framework = Path(__file__).resolve().parents[1]
        cls.artifacts = cls.framework / "storage/docker-tests"
        cls.artifacts.mkdir(exist_ok=True)
        cls.docker = Docker()
        cls.docker.control("info", "--format", "{{.ServerVersion}}")

    def fixture(self, name, mode="normal", validation_source=None, test_source=None, timeout=60, service=None):
        directory = Path(tempfile.mkdtemp(prefix=name + "-", dir=self.artifacts))
        task = _make_package(directory)
        _write_json(task / "input/value.json", {"value": 7, "mode": mode})
        if mode == "directory":
            instruction = json.loads((task / "instruction.json").read_text())
            instruction["submission"].append({"path": "model", "format": "directory", "description": "Model"})
            _write_json(task / "instruction.json", instruction)
            _write_json(task / "input/model/nested/config.json", {"value": 1})
            (task / "input/model/empty").mkdir()
            entry = task / "verifier/test/run/main.py"
            directory_check = '''model = Path(args.submission) / 'model'
assert json.loads((model / 'nested/config.json').read_text()) == {'value': 7}
assert (model / 'nested/.weights').read_bytes() == b'weights'
assert (model / 'empty').is_dir()
assert not (Path(args.submission) / 'unlisted-backup').exists()
try:
    (model / 'nested/config.json').write_text('tampered')
except OSError:
    pass
else:
    raise AssertionError('frozen directory is writable')
'''
            entry.write_text(entry.read_text().replace("observed = ", directory_check + "observed = ", 1))
        if validation_source:
            (task / "verifier/validation/run/main.py").write_text(validation_source)
        if test_source:
            (task / "verifier/test/run/main.py").write_text(test_source)
        else:
            entry = task / "verifier/test/run/main.py"
            offline_check = '''import socket
try:
    connection = socket.create_connection(('1.1.1.1',443), timeout=0.5)
except OSError:
    pass
else:
    connection.close()
    raise AssertionError('private verifier has internet access')
'''
            entry.write_text(offline_check + entry.read_text())
        script = directory / "agent.py"
        script.write_text(AGENT)
        config = directory / "config.yaml"
        value = {"task": str(task), "output": str(directory / "runs"),
                 "agent": {"type": "scripted", "script": str(script), "command": ["python3", "/agent.py"]},
                 "resources": {"timeout_seconds": timeout}}
        if service:
            value["container_service"] = service
        _write_json(config, value)
        result = run(config)
        return result, Path(result["run_directory"])

    def test_abort_stops_running_agent_without_scoring(self):
        for transport in ('socket', 'mailbox'):
            with self.subTest(transport=transport):
                result, root = self.fixture('abort-' + transport, mode='abort',
                                            service={'type': 'docker', 'action_transport': transport})
                self.assertEqual(result['status'], 'aborted', result)
                self.assertEqual(result['abort_reason'], 'Required experiment input is unavailable')
                self.assertIsNone(result['score'])
                self.assertFalse((root / 'test').exists())
                self.assertFalse((root / 'frozen-submission').exists())
                self.assertLess(result['wall_seconds'], 60)

    def test_complete_socket_flow_and_private_test(self):
        result, root = self.fixture("normal")
        self.assertEqual(result["status"], "scored", result)
        metrics = {item["name"]: item["value"] for item in result["score"]["metrics"]}
        self.assertEqual(metrics["validation_count"], 1)
        self.assertTrue((root / "submission/submission.json").is_file())
        evidence = json.loads((root / 'provenance.json').read_text())
        self.assertEqual(evidence['framework']['commit'], evidence['backend']['commit'])
        self.assertIsNotNone(evidence['framework']['commit'])
        for runtime in (root / 'agent-state/runtime.json', root / 'test/output/runtime.json'):
            observed = json.loads(runtime.read_text())
            self.assertIn('python', observed)
            self.assertIn('cgroup', observed)
        self.assertNotIn("private_score", (root / "agent-logs/stdout.log").read_text())
        context = VerifyContext(root / "context")
        self.assertEqual(context["validation_count"], 1)
        config = json.loads((root / "config.json").read_text())
        task = TaskPackage.load(json.loads((root / "task-package.json").read_text())["source"])
        before = json.loads((root / "context-before-test.json").read_text())
        scores = []
        service = DockerService(config, root)
        for ordinal in (1, 2):
            copied = VerifyContext.create(root / ("repeat-context-%d" % ordinal), config["limits"]["context_bytes"])
            copied.replace_all(before)
            verifier = Verifier(service, task, result["image"], copied.root, config, root)
            scores.append(verifier.invoke("test", root / ("repeat-%d" % ordinal), submission=root / "submission"))
        self.assertEqual(scores[0], scores[1])

    def test_remote_style_capture_in_real_image_clips_without_changing_exit_code(self):
        result, root = self.fixture('remote-capture')
        self.assertEqual(result['status'], 'scored', result)
        logs = root / 'remote-capture'
        logs.mkdir()
        writable(logs)
        command = logged_command(['python3', '-c',
                                  'import os; [(os.write(1,b"x"*65536),os.write(2,b"y"*65536)) for _ in range(32)]; exit(7)'],
                                 '/sie-logs', 1000)
        config = json.loads((root / 'config.json').read_text())
        code = self.docker.run('sie-' + root.name + '-capture', result['image'], command,
                               [(logs, '/sie-logs', False)], config['verifier'], root / 'capture-transport', 1000)
        self.assertEqual(code, 7)
        self.assertEqual((logs / 'stdout.log').read_bytes(), b'x' * 1000)
        self.assertEqual((logs / 'stderr.log').read_bytes(), b'y' * 1000)
        capture = json.loads((logs / 'capture.json').read_text())
        self.assertTrue(capture['stdout_truncated'])
        self.assertTrue(capture['stderr_truncated'])

    def test_mailbox_transport_over_docker_matches_socket(self):
        """Invariant B at a real container boundary: /control is the run directory's control/."""
        result, root = self.fixture("mailbox", service={"type": "docker", "action_transport": "mailbox", "poll_seconds": 0.2})
        self.assertEqual(result["status"], "scored", result)
        metrics = {item["name"]: item["value"] for item in result["score"]["metrics"]}
        self.assertEqual(metrics["validation_count"], 1)
        self.assertEqual(metrics["accuracy"], 1.0)
        self.assertEqual(len(list((root / "control/done").glob("*.json"))), 2)
        self.assertEqual(len(list((root / "control/responses").glob("*.json"))), 2)
        self.assertEqual(len(list((root / "control/requests").glob("*.json"))), 0)
        self.assertEqual(VerifyContext(root / "context")["validation_count"], 1)

    def test_directory_template_and_mixed_submission_reach_offline_scorer(self):
        result, root = self.fixture("directory", mode="directory")
        self.assertEqual(result["status"], "scored", result)
        self.assertEqual(result["score"]["primary_metric"]["value"], 1.0)
        self.assertEqual(sorted(p.name for p in (root / "frozen-submission").iterdir()),
                         ["model", "submission.json"])
        self.assertTrue((root / "frozen-submission/model/empty").is_dir())
        self.assertEqual((root / "frozen-submission/model/nested/.weights").read_bytes(), b"weights")
        self.assertTrue((root / "submission/unlisted-backup").exists())

    def test_missing_submission_is_distinct_from_scorer_crash(self):
        missing, _ = self.fixture("missing", mode="missing")
        broken, root = self.fixture("broken-scorer", test_source="raise RuntimeError('intentional scorer crash')\n")
        self.assertEqual(missing["status"], "invalid_submission", missing)
        self.assertEqual(broken["status"], "scorer_failed", broken)
        self.assertTrue((root / "submission/submission.json").exists())
        self.assertIn("intentional scorer crash", (root / "test/logs/stderr.log").read_text())

    def test_timeout_and_exit_without_test_do_not_score(self):
        timed, timed_root = self.fixture("timeout", mode="timeout", timeout=1)
        exited, exit_root = self.fixture("early-exit", mode="exit")
        self.assertEqual(timed["status"], "incomplete", timed)
        self.assertEqual(timed["error"], "agent_timeout")
        self.assertEqual(exited["status"], "incomplete", exited)
        self.assertFalse((timed_root / "test").exists())
        self.assertFalse((exit_root / "test").exists())

    def test_exit_137_retains_unknown_cause_and_removes_container(self):
        result, root = self.fixture('exit137', mode='exit137')
        self.assertEqual(result['status'], 'infrastructure_failed', result)
        self.assertEqual(result['failure_reason'], 'agent_exit_137_unknown')
        self.assertEqual(result['agent_exit_code'], 137)
        self.assertIsNone(result['score'])
        observed = json.loads((root / 'sandbox-observed.json').read_text())
        self.assertFalse(observed['evidence']['oom_killed'])
        self.assertEqual(result['cleanup_status'], 'completed')
        self.assertFalse((root / 'test').exists())
        self.assertFalse(self.docker.control('ps', '--all', '--filter', 'label=sie.run=' + root.name,
                                             '--format', '{{.Names}}'))

    def test_platform_deleted_agent_is_detected_and_evidence_survives(self):
        original = DockerService.start_agent
        removers, errors = [], []
        def start(service, spec):
            handle = original(service, spec)
            def remove():
                try:
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        try:
                            state = json.loads(service.docker.control('inspect', '--format', '{{json .State}}', handle.name))
                        except InfrastructureError:
                            time.sleep(0.05)  # docker run has not registered the container yet.
                            continue
                        if state.get('Running'):
                            service.docker.remove(handle.name)
                            return
                        time.sleep(0.05)
                    errors.append('agent never reached running')
                except Exception as error:
                    errors.append(str(error))
            worker = threading.Thread(target=remove)
            worker.start()
            removers.append(worker)
            return handle
        with patch.object(DockerService, 'start_agent', start):
            result, root = self.fixture('platform-deletion', mode='timeout', timeout=30)
        for worker in removers:
            worker.join(20)
            self.assertFalse(worker.is_alive())
        self.assertFalse(errors, errors)
        self.assertEqual(result['status'], 'infrastructure_failed', result)
        self.assertEqual(result['failure_reason'], 'sandbox_missing')
        self.assertIsNone(result['score'])
        self.assertEqual(result['cleanup_status'], 'completed')
        self.assertIn('sandbox_missing', (root / 'sandbox-monitor.jsonl').read_text())
        self.assertFalse((root / 'test').exists())

    def test_sigkill_controller_can_be_reclaimed_by_a_new_process_without_scoring(self):
        directory = Path(tempfile.mkdtemp(prefix='controller-kill-', dir=self.artifacts))
        task = _make_package(directory)
        config = directory / 'config.yaml'
        _write_json(config, {'task': str(task), 'output': str(directory / 'runs'),
                            'agent': {'type': 'scripted', 'command': ['python3', '-c', 'import time; time.sleep(300)']}})
        with (directory / 'controller.log').open('w') as log:
            process = subprocess.Popen([sys.executable, '-B', str(self.framework / 'tools/exam.py'),
                                        'run', '--config', str(config)], stdout=log, stderr=log)
            root = None
            try:
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline:
                    self.assertIsNone(process.poll(), (directory / 'controller.log').read_text())
                    runs = list((directory / 'runs').glob('*')) if (directory / 'runs').exists() else []
                    if runs and (runs[0] / 'resources.json').exists():
                        root = runs[0]
                        journal = ResourceJournal(root)
                        agents = [entry for key, entry in journal.pending({'docker_container'})
                                  if key.endswith('-agent') and entry['state'] == 'live']
                        if agents:
                            break
                    time.sleep(0.05)
                else:
                    self.fail('controller did not create its agent in time')
                with self.assertRaisesRegex(InfrastructureError, 'still owned'):
                    cleanup_run(root)
                (root / 'submission/preserved-evidence.txt').write_text('keep this artifact')
                process.kill()
                process.wait(10)
                # Simulate loss of the create acknowledgement: labels must find the exact object.
                journal = ResourceJournal(root)
                for key, entry in journal.pending({'docker_container'}):
                    if key.endswith('-agent'):
                        journal.update(key, state='intent', id=None)
                command = [sys.executable, '-B', str(self.framework / 'tools/exam.py'), 'cleanup', '--run', str(root)]
                recovered = subprocess.run(command, capture_output=True, text=True, timeout=90)
                self.assertEqual(recovered.returncode, 0, recovered.stdout + recovered.stderr)
                report = json.loads(recovered.stdout)
                self.assertEqual(report['cleanup_status'], 'completed')
                self.assertFalse(ResourceJournal(root).pending())
                result = json.loads((root / 'result.json').read_text())
                self.assertEqual(result['failure_reason'], 'controller_lost')
                self.assertIsNone(result['score'])
                self.assertFalse((root / 'test').exists())
                self.assertEqual((root / 'submission/preserved-evidence.txt').read_text(), 'keep this artifact')
                self.assertEqual(cleanup_run(root)['cleanup_status'], 'completed')
                self.assertFalse(self.docker.control('ps', '--all', '--filter', 'label=sie.run=' + root.name,
                                                     '--format', '{{.Names}}'))
                self.assertFalse(self.docker.control('network', 'ls', '--filter', 'label=sie.run=' + root.name,
                                                     '--format', '{{.Name}}'))
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(10)
                if root is not None:
                    cleanup_run(root)

    def test_validation_crash_rolls_back_private_state(self):
        source = 'from science_innovation_exam.verify_context import VerifyContext\nc=VerifyContext.current()\nc["charged"]=True\nraise RuntimeError("validation crash")\n'
        result, root = self.fixture("validation-failure", validation_source=source)
        self.assertEqual(result["status"], "validation_failed", result)
        self.assertEqual(VerifyContext(root / "context").snapshot(), {})

    def test_proxy_and_codex_adapter_with_local_model_stub(self):
        self.codex_with_local_model_stub()

    def test_model_fault_stops_and_cleans_real_docker_agent_without_score(self):
        for mode, reason in [('keepalive', 'first_response_timeout'), ('broken', 'upstream_incomplete_stream')]:
            with self.subTest(mode=mode):
                self.codex_with_local_model_stub(mode, reason)

    def codex_with_local_model_stub(self, fault=None, expected_reason=None):
        directory = Path(tempfile.mkdtemp(prefix="network-", dir=self.artifacts))
        task = _make_package(directory)
        bundle = directory / "bundle"
        (bundle / "bin").mkdir(parents=True)
        (bundle / "bin/codex").write_text('#!/bin/sh\nexec python3 /opt/codex/fake.py "$@"\n')
        (bundle / "bin/codex").chmod(0o755)
        client = '''import json, os, ssl, tomllib, urllib.request, urllib.error
from pathlib import Path
provider = tomllib.loads(Path('/state/config.toml').read_text())['model_providers']['sie']
request = urllib.request.Request(provider['base_url'] + '/responses', data=b'{}', headers={
    'Authorization': 'Bearer ' + os.environ['SIE_MODEL_GATEWAY_CREDENTIAL']})
assert json.loads(urllib.request.urlopen(request, timeout=10, context=ssl._create_unverified_context()).read()) == {'output': 'mock response'}
try:
    urllib.request.urlopen('http://denied.example', timeout=10)
except urllib.error.HTTPError as error:
    assert error.code == 403
else:
    raise AssertionError('denied domain was allowed')
'''
        (bundle / "fake.py").write_text(client + AGENT)
        server_file = directory / "server.py"
        server_file.write_text('''from http.server import BaseHTTPRequestHandler, HTTPServer
import ssl, time
class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        assert self.headers['Authorization'] == 'Bearer mock-model-key'
        self.rfile.read(int(self.headers.get('Content-Length', 0)))
        if FAULT:
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            if FAULT == 'broken':
                self.wfile.write(b'data: {"type":"response.created"}\\n\\n')
                self.wfile.flush()
                return
            while True:
                self.wfile.write(b': ping\\n\\n')
                self.wfile.flush()
                time.sleep(0.05)
        payload=b'{"output": "mock response"}'
        self.send_response(200)
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
server = HTTPServer(('0.0.0.0',8081),Handler)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain('/cert.pem', '/key.pem')
server.socket = context.wrap_socket(server.socket, server_side=True)
server.serve_forever()
'''.replace('FAULT', repr(fault)))
        network = "sie-stub-" + directory.name
        server_name = network + "-model"
        subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-keyout', str(directory / 'key.pem'), '-out', str(directory / 'cert.pem'),
                        '-days', '1', '-subj', '/CN=' + server_name,
                        '-addext', 'subjectAltName=DNS:' + server_name], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        (directory / 'key.pem').chmod(0o644)
        (directory / 'cert.pem').chmod(0o644)
        shutil.copyfile(directory / 'cert.pem', bundle / 'test-ca.pem')

        def launch(*args, **kwargs):
            prepared = prepare_agent(*args, **kwargs)
            prepared.environment['SSL_CERT_FILE'] = '/opt/codex/test-ca.pem'
            return prepared

        self.docker.control("network", "create", network)
        server = None
        try:
            server = self.docker.start(server_name, "python:3.11-slim-bookworm", ["python3", "/server.py"],
                                       [(server_file, "/server.py", True), (directory / "cert.pem", "/cert.pem", True),
                                        (directory / "key.pem", "/key.pem", True)], {"cpus": 1, "memory_mb": 128},
                                       directory / "model-logs", 100000, network=network, workdir="/tmp")
            config = directory / "config.yaml"
            _write_json(config, {"task": str(task), "output": str(directory / "runs"),
                                "agent": {"type": "codex", "bundle": str(bundle), "model": "mock-model"},
                                "model_monitor": {"check_seconds": 0.1, "progress_write_seconds": 0.1,
                                                  "first_response_warning_seconds": 0.3,
                                                  "first_response_timeout_seconds": 1},
                                "network": {"mode": "proxy", "denied_domains": ["denied.example"],
                                            "external_network": network, "allowed_private_hosts": [server_name],
                                            "ports": [80, 443, 8081]}})
            with patch.dict(os.environ, {"OPENAI_API_KEY": "mock-model-key", "OPENAI_BASE_URL": "https://%s:8081/v1" % server_name}), \
                    patch('science_innovation_exam.runner.prepare_agent', side_effect=launch):
                result = run(config)
            self.assertEqual(result["status"], "infrastructure_failed" if fault else "scored", result)
            root = Path(result["run_directory"])
            access = (root / "network/state/access.jsonl").read_text()
            self.assertIn(server_name, access)
            if fault:
                self.assertIsNone(result['score'])
                self.assertEqual(result['failure_phase'], 'model_api')
                self.assertEqual(result['failure_reason'], expected_reason)
                self.assertEqual(result['cleanup_status'], 'completed')
                self.assertFalse((root / 'test').exists())
                self.assertEqual(self.docker.control('ps', '-a', '--filter', 'name=sie-' + root.name,
                                                     '--format', '{{.Names}}'), '')
            else:
                self.assertIn("domain is denied", access)
            self.assertNotIn("mock-model-key", (root / "config.json").read_text())
        finally:
            self.docker.remove(server_name)
            if server:
                server.wait(10)
            self.docker.control("network", "rm", network)

    def test_zz_no_live_test_containers(self):
        self.assertEqual(self.docker.control("ps", "--filter", "name=sie-", "--format", "{{.Names}}"), "")
