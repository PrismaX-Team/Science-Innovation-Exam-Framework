"""Platform loss, intermittent observation and ambiguous process termination."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from science_innovation_exam import runner
from science_innovation_exam.common.errors import ContractError, InfrastructureError
from science_innovation_exam.config import load_config
from science_innovation_exam.container_service.docker import DockerHandle
from science_innovation_exam.container_service.interface import AgentObservation
from science_innovation_exam.sandbox_monitor import SANDBOX_DEFAULTS, SandboxFault, SandboxMonitor
from tests.helpers import _make_package, _write_json
from tests.test_container_service import FakeContainerService, FakeHandle


def options(**extra):
    return {**SANDBOX_DEFAULTS, 'check_seconds': 0.01, 'query_timeout_seconds': 0.05,
            'warning_seconds': 0.1, 'timeout_seconds': 0.3, 'recovery_seconds': 0.1, **extra}


class SandboxMonitorTests(unittest.TestCase):
    def test_continuous_and_intermittent_loss_cannot_reset_grace_forever(self):
        for alternating in (False, True):
            with self.subTest(alternating=alternating), tempfile.TemporaryDirectory() as temporary:
                clock = [0.0]
                handle = Mock()
                handle.observe.side_effect = lambda _: AgentObservation(
                    'running' if alternating and round(clock[0] * 20) % 2 else 'unknown')
                with patch('science_innovation_exam.sandbox_monitor.time.monotonic', side_effect=lambda: clock[0]):
                    monitor = SandboxMonitor(handle, Path(temporary), options())
                    with self.assertRaises(SandboxFault) as failure:
                        for step in range(10):
                            clock[0] = step * 0.05
                            monitor.check(force=True)
                    self.assertEqual(failure.exception.reason, 'sandbox_status_unstable')
                    self.assertLessEqual(clock[0], 0.35)
                    self.assertIn('warning', (Path(temporary) / 'sandbox-monitor.jsonl').read_text())

    def test_sustained_recovery_starts_a_new_outage_episode(self):
        with tempfile.TemporaryDirectory() as temporary:
            clock, handle = [0.0], Mock()
            handle.observe.return_value = AgentObservation('unknown')
            with patch('science_innovation_exam.sandbox_monitor.time.monotonic', side_effect=lambda: clock[0]):
                monitor = SandboxMonitor(handle, Path(temporary), options())
                monitor.check(force=True)
                handle.observe.return_value = AgentObservation('running')
                for moment in (0.05, 0.16):
                    clock[0] = moment
                    monitor.check(force=True)
                self.assertFalse(monitor.summary()['unstable'])
                clock[0] = 0.5
                handle.observe.return_value = AgentObservation('unknown')
                monitor.check(force=True)
                self.assertEqual(monitor.unhealthy_since, 0.5)

    def test_blocked_reader_is_bounded_and_does_not_accumulate_threads(self):
        with tempfile.TemporaryDirectory() as temporary:
            stop, handle = threading.Event(), Mock()
            handle.observe.side_effect = lambda _: stop.wait(2)
            monitor = SandboxMonitor(handle, Path(temporary), options(query_timeout_seconds=0.02,
                                      warning_seconds=0.02, timeout_seconds=0.06))
            began = time.monotonic()
            try:
                with self.assertRaises(SandboxFault):
                    while time.monotonic() - began < 1:
                        monitor.check(force=True)
                self.assertEqual(handle.observe.call_count, 1)
                self.assertLess(time.monotonic() - began, 0.5)
            finally:
                stop.set()

    def test_late_and_invalid_results_do_not_claim_health(self):
        with tempfile.TemporaryDirectory() as temporary:
            handle = Mock()
            handle.observe.side_effect = lambda _: (time.sleep(0.04), AgentObservation('running'))[1]
            monitor = SandboxMonitor(handle, Path(temporary), options(query_timeout_seconds=0.01))
            self.assertEqual(monitor.check(force=True).state, 'unknown')
            time.sleep(0.05)
            self.assertEqual(monitor.check(force=True).state, 'unknown')
            for value in ({}, AgentObservation('exited'), AgentObservation('running', evidence={'x': float('nan')})):
                handle.observe.side_effect = None
                handle.observe.return_value = value
                self.assertEqual(monitor.check(force=True).reason, 'invalid_status_observation')

    def test_docker_inspect_crosscheck_and_oom_are_distinct(self):
        docker, process = Mock(), Mock()
        process.poll.return_value = None
        handle = DockerHandle(docker, 'sie-owned-agent', process, None, None, None)
        docker.control.return_value = json.dumps({'Running': True, 'OOMKilled': False})
        self.assertEqual(handle.observe(1).state, 'running')
        docker.control.return_value = json.dumps({'Running': False, 'OOMKilled': True, 'ExitCode': 137})
        self.assertEqual(handle.observe(1).reason, 'sandbox_oom_killed')
        docker.control.return_value = json.dumps({'Running': False, 'Status': 'exited', 'ExitCode': 137})
        self.assertEqual(handle.observe(1).state, 'exited')
        docker.control.side_effect = [InfrastructureError('inspect failed'), '']
        self.assertEqual(handle.observe(1).state, 'missing')
        handle.seen = False
        docker.control.side_effect = [InfrastructureError('inspect failed'), '']
        self.assertEqual(handle.observe(1).reason, 'docker_starting')
        docker.control.side_effect = [InfrastructureError('inspect failed'), InfrastructureError('engine down')]
        with self.assertRaises(InfrastructureError):
            handle.observe(1)

    def test_runner_preserves_evidence_null_score_and_pending_cleanup(self):
        for state, code, reason in [('missing', None, 'sandbox_missing'),
                                    ('exited', 137, 'agent_exit_137_unknown'),
                                    ('failed', 137, 'sandbox_oom_killed')]:
            with self.subTest(state=state), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                task = _make_package(root)
                path = root / 'config.yaml'
                _write_json(path, {'task': str(task), 'output': str(root / 'runs'),
                                  'agent': {'type': 'scripted', 'command': ['true']}})
                instances = []
                class Handle(FakeHandle):
                    def observe(self, timeout_seconds):
                        return AgentObservation(state, code, reason, {'source': 'fake'})
                class Backend(FakeContainerService):
                    def start_agent(self, spec):
                        return Handle(self.log)
                    def cleanup(self):
                        super().cleanup()
                        raise OSError('delete not confirmed')
                def load(config, directory):
                    backend = Backend(config, directory)
                    instances.append(backend)
                    return backend
                result = runner.run(path, load_service=load, prepare_image=lambda *a, **k: ('fake', 'unused'))
                self.assertEqual(result['failure_reason'], reason)
                self.assertEqual(result['failure_phase'], 'sandbox')
                self.assertEqual(result['status'], 'infrastructure_failed')
                self.assertIsNone(result['score'])
                self.assertEqual(result['cleanup_status'], 'pending')
                self.assertIn('stop', instances[0].log)
                self.assertNotIn('run_verifier:test', instances[0].log)
                self.assertTrue((Path(result['run_directory']) / 'sandbox-observed.json').exists())

    def test_config_rejects_invalid_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'config.yaml'
            for invalid in ({'check_seconds': False}, {'query_timeout_seconds': float('inf')},
                            {'recovery_seconds': 0}, {'typo': 1}, {'warning_seconds': 121}):
                _write_json(path, {'task': 'unused', 'output': 'runs', 'sandbox_monitor': invalid,
                                  'agent': {'type': 'scripted', 'command': ['true']}})
                with self.assertRaises(ContractError):
                    load_config(path)

    def test_terminal_request_cannot_score_after_platform_disappears_or_becomes_unknown(self):
        for state in ('missing', 'unknown'):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                task = _make_package(root)
                path = root / 'config.yaml'
                _write_json(path, {'task': str(task), 'output': str(root / 'runs'),
                                  'agent': {'type': 'scripted', 'command': ['true']}})
                class Handle(FakeHandle):
                    def observe(self, _timeout):
                        return AgentObservation(state, reason='sandbox_missing' if state == 'missing' else '')
                class Backend(FakeContainerService):
                    def start_agent(self, spec):
                        super().start_agent(spec)  # Produces a valid test request and files.
                        return Handle(self.log)
                backend = []
                def load(config, directory):
                    backend.append(Backend(config, directory))
                    return backend[-1]
                result = runner.run(path, load_service=load,
                                    prepare_image=lambda *a, **k: ('fake', 'sie-cpu:local-tag'))
                self.assertEqual(result['status'], 'infrastructure_failed')
                self.assertIsNone(result['score'])
                self.assertNotIn('run_verifier:test', backend[0].log)
                self.assertEqual(result['failure_reason'], 'sandbox_missing' if state == 'missing'
                                 else 'sandbox_status_unavailable_at_handoff')
