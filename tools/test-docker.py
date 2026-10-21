#!/usr/bin/env python3
"""Run only real Docker tests. No remote model requests."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(root / "src"), str(root)]
runtime = sorted((root / "storage/controller-runtime").glob("cpython-%s%s-*/site-packages" % sys.version_info[:2]))
if runtime:
    sys.path.insert(1, str(runtime[-1]))
os.environ["SIE_DOCKER_TESTS"] = "1"
scratch = root / "storage/test-tmp"
scratch.mkdir(exist_ok=True)
tempfile.tempdir = str(scratch)
suite = unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:] or ["tests.test_docker"])
result = unittest.TextTestRunner(verbosity=2).run(suite)
raise SystemExit(not result.wasSuccessful())
