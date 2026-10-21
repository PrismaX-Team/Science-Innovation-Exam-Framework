"""Private process entry for task-package custom Matrix transforms."""

import argparse
import importlib.util
import json
import sys
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any, Mapping


# This file is also executed directly with Python isolated mode. Insert the
# checked-in/installed source root explicitly before importing the framework.
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from science_innovation_exam.common.serialization import atomic_write_json  # noqa: E402


def _load_transform(path: Path):
    unresolved = path.absolute()
    if unresolved.is_symlink():
        raise ValueError("custom Matrix entry cannot be a symlink")
    path = unresolved.resolve()
    if tuple(path.parts[-3:]) != ("verifier", "matrix", "transform.py"):
        raise ValueError("custom entry must be verifier/matrix/transform.py")
    if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
        raise ValueError("custom entry is missing, unsafe, or empty")
    vendor = path.parent / "vendor"
    if vendor.exists():
        if not vendor.is_dir() or vendor.is_symlink():
            raise ValueError("custom Matrix vendor path must be a real directory")
        sys.path.insert(0, str(vendor))
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location("_sie_task_matrix_transform", str(path))
    if spec is None or spec.loader is None:
        raise ValueError("cannot load custom Matrix module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    function = getattr(module, "to_utility", None)
    if not callable(function):
        raise ValueError("custom Matrix module must define callable to_utility")
    return function


def _request(path: Path) -> Mapping[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "raw_values",
        "parameters",
        "precision",
    }:
        raise ValueError("custom Matrix request has invalid fields")
    if value["schema_version"] != "1.0":
        raise ValueError("unsupported custom Matrix request version")
    if not isinstance(value["raw_values"], list) or not value["raw_values"]:
        raise ValueError("custom Matrix request requires raw values")
    if not isinstance(value["parameters"], Mapping):
        raise ValueError("custom Matrix parameters must be an object")
    precision = value["precision"]
    if (
        isinstance(precision, bool)
        or not isinstance(precision, int)
        or not 16 <= precision <= 1000
    ):
        raise ValueError("custom Matrix precision is invalid")
    return value


def execute(transform_path: Path, request_path: Path, result_path: Path) -> None:
    request = _request(request_path)
    function = _load_transform(transform_path)
    values = []
    with localcontext() as context:
        context.prec = int(request["precision"])
        for item in request["raw_values"]:
            if not isinstance(item, str):
                raise ValueError("custom Matrix raw values must be decimal strings")
            raw_value = Decimal(item)
            if not raw_value.is_finite():
                raise ValueError("custom Matrix raw values must be finite")
            utility = function(raw_value, request["parameters"])
            if not isinstance(utility, Decimal) or not utility.is_finite():
                raise TypeError("to_utility must return a finite Decimal")
            values.append(str(utility.copy_abs() if utility.is_zero() else utility))
    atomic_write_json(
        result_path,
        {"schema_version": "1.0", "status": "ok", "utility_values": values},
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--transform", required=True, type=Path)
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--result", required=True, type=Path)
    args = parser.parse_args()
    try:
        execute(args.transform, args.request, args.result)
    except Exception as error:
        message = "%s: %s" % (error.__class__.__name__, error)
        sys.stderr.write(message[:2000] + "\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
