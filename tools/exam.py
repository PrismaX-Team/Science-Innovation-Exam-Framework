#!/usr/bin/env python3
"""Run from a checkout, optionally using the existing local dependency installation."""
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root / "src"))
runtime = sorted((root / "storage/controller-runtime").glob(
    "cpython-%s%s-*/site-packages" % sys.version_info[:2]))
if runtime:
    sys.path.insert(1, str(runtime[-1]))
from science_innovation_exam.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
