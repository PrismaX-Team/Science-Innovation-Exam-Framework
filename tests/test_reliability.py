"""Regressions for movable plugins, retained results and confirmed cleanup."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from science_innovation_exam import container_service, runner
from science_innovation_exam.common.errors import ContractError, InfrastructureError
from science_innovation_exam.config import load_config
from science_innovation_exam.container_service.docker import Docker
from tests import test_container_service as services
from tests.helpers import _make_package, _write_json


class ReliabilityTests(unittest.TestCase):
    def config(self, root, **extra):
        task = _make_package(root)
        value = {"task": str(task), "output": str(root / "runs"),
                 "agent": {"type": "scripted", "command": ["python3", "-c", "pass"]}}
        value.update(extra)
        path = root / "config.yaml"
        _write_json(path, value)
        return path

    def test_yaml_locates_a_plugin_outside_the_framework_and_supports_lazy_imports(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(sys, "path", sys.path[:]):
            root = Path(temporary)
            config_dir, plugins = root / "configs", root / "somewhere/plug-ins"
            config_dir.mkdir()
            package = plugins / "movable_test_backend"
            package.mkdir(parents=True)
            (package / "__init__.py").write_text(
                "from science_innovation_exam.container_service.interface import ContainerService\n"
                "class Service(ContainerService):\n"
                "    def check(self):\n"
                "        from .lazy import VALUE\n"
                "        return VALUE\n"
                "def create(config, run_dir):\n"
                "    assert 'module_path' not in config['container_service']\n"
                "    return Service(config, run_dir)\n")
            (package / "lazy.py").write_text("VALUE = 42\n")
            path = self.config(config_dir, container_service={"type": "external", "module": "movable_test_backend",
                                                               "module_path": "../somewhere/plug-ins"})
            try:
                config = load_config(path)
                self.assertEqual(config['container_service']['module_path'], str(plugins))
                self.assertEqual(container_service.load(config, root).check(), 42)
            finally:
                for name in list(sys.modules):
                    if name.startswith('movable_test_backend'):
                        del sys.modules[name]

    def test_missing_plugin_directory_and_cached_module_from_wrong_directory_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(sys, "path", sys.path[:]):
            root = Path(temporary)
            path = self.config(root, container_service={"type": "external", "module": "json", "module_path": "missing"})
            with self.assertRaisesRegex(ContractError, 'directory does not exist'):
                load_config(path)
            (root / 'missing').mkdir()
            with self.assertRaisesRegex(ContractError, 'outside module_path'):
                container_service.load(load_config(path), root)
            (root / 'missing/broken_backend.py').write_text('create = None\n')
            config = load_config(path)
            config['container_service']['module'] = 'broken_backend'
            try:
                with self.assertRaisesRegex(ContractError, 'must define create'):
                    container_service.load(config, root)
            finally:
                sys.modules.pop('broken_backend', None)

    def test_metadata_and_unsafe_log_failures_do_not_lose_the_score_or_touch_link_targets(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = self.config(root)
            secret = 'dummy-quote"-key'
            outside = root / 'outside.txt'
            outside.write_text(secret)
            original_prepare = runner.prepare_agent

            def prepare(*args, **kwargs):
                launch = original_prepare(*args, **kwargs)
                launch.secrets = [secret]
                return launch

            class Backend(services.FakeContainerService):
                def start_agent(self, spec):
                    handle = super().start_agent(spec)
                    (self.run_dir / 'linked.log').symlink_to(outside)
                    (self.run_dir / 'dangling.log').symlink_to(root / 'absent')
                    os.mkfifo(self.run_dir / 'pipe.log')
                    (self.run_dir / 'normal.log').write_text(secret)
                    return handle

                def run_metadata(self):
                    raise OSError('metadata failure: ' + secret)

            with patch.object(runner, 'prepare_agent', side_effect=prepare):
                result = runner.run(path, load_service=Backend,
                                    prepare_image=lambda *a, **k: ('sha256:fake', 'sie-cpu:local-tag'))
            run_dir = Path(result['run_directory'])
            saved = json.loads((run_dir / 'result.json').read_text())
            self.assertEqual(saved, result)
            self.assertEqual(result['status'], 'scored')
            self.assertEqual(result['score']['primary_metric']['value'], 1)
            self.assertTrue(result['diagnostic_errors'])
            self.assertNotIn(secret, json.dumps(result))
            self.assertEqual(outside.read_text(), secret)
            self.assertEqual((run_dir / 'normal.log').read_text(), '[REDACTED]')

    def test_docker_cleanup_confirms_absence_and_propagates_query_failures(self):
        for names, query_code, fails in (('', 0, False), ('sie-test-agent\n', 0, True), ('', 1, True)):
            with self.subTest(names=names, query_code=query_code), patch('subprocess.run', side_effect=[
                    subprocess.CompletedProcess([], 1, '', 'remove failed or already absent'),
                    subprocess.CompletedProcess([], query_code, names, 'query failed')]):
                if fails:
                    with self.assertRaises(InfrastructureError):
                        Docker().remove('sie-test-agent')
                else:
                    Docker().remove('sie-test-agent')

    def test_agent_timeout_starts_after_preparation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = self.config(root, resources={'timeout_seconds': 3})
            clock, polls = [0.0], []

            class Handle(services.FakeHandle):
                def poll(self):
                    clock[0] += 1
                    polls.append(clock[0])
                    return None

            class Backend(services.FakeContainerService):
                def prepare_task(self, task):
                    clock[0] += 100  # Provisioning took much longer than the experiment budget.

                def start_agent(self, spec):
                    return Handle(self.log)

            class Observer:
                # Test budget accounting independently of filesystem latency and
                # observer threads, which have their own controlled fault tests.
                def __init__(self, handle, *_args):
                    self.handle, self.latest = handle, None
                def check(self, **_kwargs):
                    self.latest = self.handle.observe(1)
                    return self.latest
                def summary(self):
                    return {}

            with patch.object(runner.time, 'monotonic', side_effect=lambda: clock[0]), \
                 patch.object(runner, 'SandboxMonitor', Observer):
                result = runner.run(path, load_service=Backend,
                                    prepare_image=lambda *a, **k: ('sha256:fake', 'sie-cpu:local-tag'))
            self.assertEqual(result['status'], 'incomplete')
            self.assertEqual(result['error'], 'agent_timeout')
            self.assertEqual(polls, [101, 102, 103])
            self.assertEqual(result['preparation_seconds'], 100)
            self.assertEqual(result['agent_seconds'], 3)

    def test_unserializable_metadata_and_log_scan_failure_are_diagnostics(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = self.config(root)

            class Backend(services.FakeContainerService):
                def run_metadata(self):
                    return {'unserializable': object()}

            with patch.object(runner, 'redact_logs', side_effect=OSError('scan failed')):
                result = runner.run(path, load_service=Backend,
                                    prepare_image=lambda *a, **k: ('sha256:fake', 'sie-cpu:local-tag'))
            saved = json.loads((Path(result['run_directory']) / 'result.json').read_text())
            self.assertEqual(saved['status'], 'scored')
            self.assertEqual(len(saved['diagnostic_errors']), 2)
