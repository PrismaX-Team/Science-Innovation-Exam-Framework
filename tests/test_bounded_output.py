"""Real subprocess floods and hostile files; no Docker or external service."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from science_innovation_exam.bounded_output import run, OutputLimitError
from science_innovation_exam.common.bounded_io import read_bytes, redact_file


class BoundedOutputTests(unittest.TestCase):
    def test_both_streams_are_drained_and_exit_status_is_preserved_after_clipping(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            argv = [sys.executable, '-c',
                    'import os; [(os.write(1,b"x"*65536),os.write(2,b"y"*65536)) for _ in range(64)]; exit(7)']
            result = run(argv, 1000, timeout=10, directory=root)
            self.assertEqual(result.returncode, 7)
            self.assertEqual((root / 'stdout.log').read_bytes(), b'x' * 1000)
            self.assertEqual((root / 'stderr.log').read_bytes(), b'y' * 1000)
            with self.assertRaises(OutputLimitError):
                run(argv, 1000, timeout=10)

    def test_input_output_and_deadline(self):
        data = b'a' * 200000
        result = run([sys.executable, '-c', 'import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())'],
                     len(data), timeout=5, input=data)
        self.assertEqual(result.stdout, data)
        with self.assertRaises(subprocess.TimeoutExpired):
            run([sys.executable, '-c', 'import time; time.sleep(30)'], 64, timeout=0.1)

    def test_bounded_read_does_not_rely_on_a_prior_size_check(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / 'output'
            path.write_bytes(b'x' * 17)
            with self.assertRaises(ValueError):
                read_bytes(path, 16)
            path.unlink()
            os.mkfifo(path)
            with self.assertRaises(ValueError):
                read_bytes(path, 16)
            path.unlink()
            target = root / 'target'
            target.write_bytes(b'keep')
            path.symlink_to(target)
            with self.assertRaises(OSError):
                read_bytes(path, 16)
            with self.assertRaises(OSError):
                redact_file(path, ['keep'], 16)
            self.assertEqual(target.read_bytes(), b'keep')

    def test_redaction_handles_boundary_overlap_and_truncation(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'output.log'
            secret = 'credential-very-private'
            prefix = b'x' * (65536 - 5)
            original = prefix + secret.encode() + b' end ' + secret.encode()
            path.write_bytes(original)
            redact_file(path, [secret], 200000)
            self.assertEqual(path.read_bytes(), original.replace(secret.encode(), b'[REDACTED]'))
            path.write_bytes(original)
            redact_file(path, [secret], len(prefix) + 5)
            self.assertEqual(path.read_bytes(), (prefix + b'[REDACTED]')[:len(prefix) + 5])

    def test_redaction_never_issues_an_unbounded_read(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'large.log'
            path.write_bytes(b'x' * 150000)
            original = os.fdopen
            class Reader:
                def __init__(self, fd): self.handle = original(fd, 'rb')
                def __enter__(self): return self
                def __exit__(self, *args): self.handle.close()
                def fileno(self): return self.handle.fileno()
                def read(self, count):
                    self_case.assertLessEqual(count, 65536)
                    return self.handle.read(count)
            self_case = self
            with patch('science_innovation_exam.common.bounded_io.os.fdopen',
                       side_effect=lambda fd, mode: Reader(fd) if mode == 'rb' else original(fd, mode)):
                redact_file(path, ['absent-secret'], 100000)
            self.assertEqual(path.stat().st_size, 100000)
