"""Durable intents, live-owner exclusion and safe, repeatable post-crash cleanup."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from science_innovation_exam.common.errors import InfrastructureError
from science_innovation_exam.common.serialization import atomic_write_json
from science_innovation_exam.container_service.docker import Docker
from science_innovation_exam.container_service.interface import ContainerService
from science_innovation_exam.recovery import cleanup_run
from science_innovation_exam.resources import ResourceJournal, RunLock
from science_innovation_exam import owned_command


class RecoveryTests(unittest.TestCase):
    def fixture(self, root):
        config = {'container_service': {'type': 'docker'}}
        atomic_write_json(root / 'config.json', config)
        return ResourceJournal.create(root, config['container_service'])

    def test_sigkill_releases_owner_lock_and_never_requires_pid_killing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            source = ('import sys,time\nfrom pathlib import Path\n'
                      'from science_innovation_exam.resources import RunLock\n'
                      'with RunLock(Path(sys.argv[1])):\n'
                      ' Path(sys.argv[1],"ready").touch()\n time.sleep(30)\n')
            environment = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / 'src'))
            process = subprocess.Popen([sys.executable, '-c', source, str(root)], env=environment)
            try:
                deadline = time.monotonic() + 5
                while not (root / 'ready').exists():
                    self.assertIsNone(process.poll())
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.01)
                with self.assertRaisesRegex(InfrastructureError, 'still owned'):
                    with RunLock(root, 'cleanup'):
                        self.fail('live controller lock was stolen')
                os.kill(process.pid, signal.SIGKILL)
                process.wait(5)
                with RunLock(root, 'cleanup'):
                    self.assertEqual(json.loads((root / 'controller.json').read_text())['operation'], 'cleanup')
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(5)

    def test_other_host_and_copied_run_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            journal = self.fixture(root)
            atomic_write_json(root / 'controller.json', {'host': 'some-other-machine'})
            with self.assertRaisesRegex(InfrastructureError, 'originating'):
                with RunLock(root, 'cleanup'):
                    pass
            journal.data['run_directory'] = '/another/directory'
            journal.save()
            with self.assertRaises(InfrastructureError):
                ResourceJournal(root)

    def test_write_failure_prevents_create_intent_and_reuse_requires_removal(self):
        with tempfile.TemporaryDirectory() as temporary:
            journal = self.fixture(Path(temporary))
            with patch('science_innovation_exam.resources.atomic_write_json', side_effect=OSError('disk full')):
                with self.assertRaises(OSError):
                    journal.intent('first', 'docker_container', {'name': 'owned'})
            restored = ResourceJournal(Path(temporary))
            self.assertFalse(restored.pending())
            original = restored.intent('first', 'docker_container', {'name': 'owned'})
            with self.assertRaises(InfrastructureError):
                restored.intent('first', 'docker_container', {'name': 'owned'})
            restored.removed('first')
            again = restored.intent('first', 'docker_container', {'name': 'owned'})
            self.assertNotEqual(original['request'], again['request'])

    def test_lost_creation_response_reconciles_labels_and_immutable_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            journal = self.fixture(Path(temporary))
            name = 'sie-owned-agent'
            key = 'docker:container:' + name
            entry = journal.intent(key, 'docker_container', {'name': name})
            docker = Docker(journal)
            record = {'id': 'immutable-id', 'labels': {'sie.run': journal.data['run_id'],
                      'sie.owner': journal.owner, 'sie.request': entry['request']}}
            docker._identity = Mock(side_effect=[record, None])
            docker.control = Mock()
            docker.remove_owned('container', name)
            docker.control.assert_called_once_with('rm', '--force', 'immutable-id', check=False, timeout=60)
            self.assertEqual(ResourceJournal(Path(temporary)).pending(), [])
            docker.remove_owned('container', name)
            self.assertEqual(docker.control.call_count, 1)

    def test_unknown_absence_foreign_labels_and_reused_id_never_delete(self):
        for failure in ('unknown_absence', 'foreign', 'reused_id'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                journal = self.fixture(Path(temporary))
                name = 'sie-owned-agent'
                key = 'docker:container:' + name
                entry = journal.intent(key, 'docker_container', {'name': name})
                record = {'id': 'new-id', 'labels': {'sie.run': journal.data['run_id'],
                          'sie.owner': journal.owner, 'sie.request': entry['request']}}
                if failure == 'foreign':
                    record['labels']['sie.owner'] = 'someone-else'
                if failure == 'reused_id':
                    journal.update(key, state='live', id='old-id')
                docker = Docker(journal)
                docker._identity = Mock(return_value=None if failure == 'unknown_absence' else record)
                docker.control = Mock()
                with self.assertRaises(InfrastructureError):
                    docker.remove_owned('container', name)
                docker.control.assert_not_called()
                self.assertTrue(journal.pending())

    def test_partial_cleanup_retry_preserves_historical_result_and_submission(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            journal = self.fixture(root)
            for key in ('one', 'two'):
                journal.intent(key, 'fake', {})
            original = {'status': 'infrastructure_failed', 'score': None, 'error': 'old evidence',
                        'cleanup_status': 'pending', 'cleanup_errors': ['original failure']}
            atomic_write_json(root / 'result.json', original)
            (root / 'submission').mkdir()
            (root / 'submission/evidence').write_text('keep')
            attempts = []
            class Service(ContainerService):
                def recover_resources(self):
                    attempts.append(1)
                    self.resources.removed('one')
                    if len(attempts) == 1:
                        raise OSError('SECRET must not be persisted')
                    self.resources.removed('two')
            first = cleanup_run(root, load_service=Service)
            self.assertEqual(first['cleanup_status'], 'pending')
            self.assertEqual(first['pending_resources'], ['two'])
            second = cleanup_run(root, load_service=Service)
            self.assertEqual(second['cleanup_status'], 'completed')
            saved = json.loads((root / 'result.json').read_text())
            for key, value in original.items():
                self.assertEqual(saved[key], value)
            self.assertNotIn('SECRET', (root / 'cleanup-result.json').read_text())
            self.assertEqual((root / 'submission/evidence').read_text(), 'keep')

    def test_missing_result_records_controller_loss_without_a_score(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.fixture(root)
            class Service(ContainerService):
                def recover_resources(self):
                    pass
            self.assertEqual(cleanup_run(root, load_service=Service)['status'], 'cleaned')
            result = json.loads((root / 'result.json').read_text())
            self.assertEqual(result['failure_reason'], 'controller_lost')
            self.assertIsNone(result['score'])

    def test_changed_backend_scope_and_symlink_records_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            journal = self.fixture(root)
            journal.bind('docker_engine', 'original')
            with self.assertRaises(InfrastructureError):
                journal.bind('docker_engine', 'another-daemon')
            (root / 'resources.json').rename(root / 'outside')
            (root / 'resources.json').symlink_to(root / 'outside')
            with self.assertRaises(OSError):
                ResourceJournal(root)

    def test_parent_death_guard_stops_an_orphan_mutating_writer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            record, output = root / 'operation.json', root / 'writes'
            writer = 'import sys,time\nwhile True:\n open(sys.argv[1],"a").write("x")\n time.sleep(.01)\n'
            parent_source = ('import os,subprocess,sys\nsubprocess.run([sys.executable,sys.argv[1],'
                             'str(os.getpid()),sys.argv[2],sys.executable,"-c",sys.argv[3],sys.argv[4]])')
            process = subprocess.Popen([sys.executable, '-c', parent_source, owned_command.__file__,
                                        str(record), writer, str(output)])
            try:
                deadline = time.monotonic() + 5
                while not output.exists():
                    self.assertIsNone(process.poll())
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.01)
                identity = json.loads(record.read_text())
                self.assertTrue(owned_command.still_running(identity))
                process.kill()
                process.wait(5)
                deadline = time.monotonic() + 5
                while owned_command.still_running(identity):
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(0.01)
                size = output.stat().st_size
                time.sleep(0.05)
                self.assertEqual(output.stat().st_size, size)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(5)
