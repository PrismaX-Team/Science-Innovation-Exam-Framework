"""Source status, missing metadata and the real exec prelude."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from science_innovation_exam import provenance, runner
from science_innovation_exam.common.errors import ContractError
from science_innovation_exam.config import load_config
from tests import test_reliability as reliability, test_container_service as services


class ProvenanceTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('git'), 'Git executable required')
    def test_clean_dirty_untracked_and_rename_are_recorded_without_contents_or_remotes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            def git(*args):
                return subprocess.run(['git', '-C', str(root), '-c', 'core.hooksPath=/dev/null',
                                       '-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.invalid'] + list(args),
                                      check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            git('init')
            (root / 'code.py').write_text('original')
            (root / 'src/package').mkdir(parents=True)
            (root / 'src/package/__init__.py').write_text('nested source')
            git('add', 'code.py')
            git('add', 'src')
            git('commit', '-m', 'fixture')
            clean = provenance.source_record(root)
            self.assertFalse(clean['dirty'])
            self.assertEqual(len(clean['commit']), 40)
            nested = provenance.source_record(root / 'src/package/__init__.py')
            self.assertTrue(nested['source_tracked'])
            self.assertEqual(nested['commit'], clean['commit'])
            git('mv', 'code.py', 'renamed.py')
            (root / 'private-untracked').write_text('SECRET CONTENT')
            git('remote', 'add', 'origin', 'https://SECRET_TOKEN@example.invalid/repo')
            dirty = provenance.source_record(root)
            self.assertTrue(dirty['dirty'])
            self.assertEqual(dirty['tracked_changes'], 1)
            self.assertEqual(dirty['untracked_entries'], 1)
            self.assertNotIn('SECRET', json.dumps(dirty))
            self.assertNotIn('private-untracked', json.dumps(dirty))
            (root / '.gitignore').write_text('ignored-copy/\n')
            copy = root / 'ignored-copy'
            copy.mkdir()
            (copy / 'input.txt').write_text('unversioned task copy')
            copied = provenance.source_record(copy)
            self.assertFalse(copied['source_tracked'])
            self.assertIsNone(copied['commit'])
            self.assertEqual(copied['repository_commit'], clean['commit'])
            limited = provenance.source_record(root, maximum_bytes=1)
            self.assertIsNone(limited['dirty'])
            self.assertIn('observation_error', limited)

    def test_non_git_directory_has_unknown_revision(self):
        # storage/ is inside the framework checkout, so use a mocked failed Git
        # command rather than accidentally treating its parent repo as absent.
        with patch.object(provenance, 'capture', return_value=subprocess.CompletedProcess([], 128, b'', b'failure')):
            record = provenance.source_record(Path.cwd())
        self.assertIsNone(record['commit'])
        self.assertIsNone(record['dirty'])

    def test_exec_prelude_saves_runtime_and_preserves_exit_code_without_environment_dump(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'runtime.json'
            env = dict(os.environ, PROVENANCE_TEST_SECRET='NEVER_RECORD_THIS',
                       PYTHONPATH=str(Path(provenance.__file__).resolve().parents[1]))
            argv = provenance.command([sys.executable, '-c', 'exit(7)'], str(path),
                                      {'timeout_seconds': 1, 'maximum_bytes': 1048576})
            result = subprocess.run(argv, env=env, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 7, result.stderr)
            value = json.loads(path.read_text())
            self.assertIn('python', value)
            self.assertIn('cgroup', value)
            self.assertNotIn('NEVER_RECORD_THIS', path.read_text())

    def test_run_preserves_source_evidence_even_when_backend_initialization_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = reliability.ReliabilityTests().config(Path(temporary))
            def fail(*_args): raise RuntimeError('backend unavailable')
            result = runner.run(path, load_service=fail)
            evidence = json.loads((Path(result['run_directory']) / 'provenance.json').read_text())
            self.assertEqual(result['status'], 'infrastructure_failed')
            self.assertIn('framework', evidence)
            self.assertEqual(evidence['task']['fingerprint'],
                             runner.TaskPackage.load(load_config(path)['task']).fingerprint())

    def test_metadata_failure_is_diagnostic_and_config_limits_are_validated(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = reliability.ReliabilityTests().config(Path(temporary))
            with patch.object(provenance, 'collect', side_effect=OSError('unavailable')):
                result = runner.run(path, load_service=services.FakeContainerService,
                                    prepare_image=lambda *a, **k: ('sha256:fake', 'sie-cpu:local-tag'))
            self.assertEqual(result['status'], 'scored')
            self.assertIn('provenance: OSError', result['diagnostic_errors'])
            for options in ({'timeout_seconds': 0}, {'maximum_bytes': 1.5}, {'unexpected': 1}):
                value = json.loads(path.read_text())
                value['provenance'] = options
                path.write_text(json.dumps(value))
                with self.assertRaises(ContractError):
                    load_config(path)
