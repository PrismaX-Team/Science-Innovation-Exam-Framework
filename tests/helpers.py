import json
from pathlib import Path

def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def _instruction() -> dict:
    return {
        "instructions": "Read input/value.json and submit the same integer.",
        "input": [
            {
                "path": "value.json",
                "format": "json",
                "description": "One public integer.",
            }
        ],
        "submission": [
            {
                "path": "submission.json",
                "format": "json",
                "description": "The submitted integer.",
                "has_template": False,
            }
        ],
        "environment": [],
        "requirement": [],
        "limitation": {},
    }


def _meta() -> dict:
    return {
        "id": "integer_echo",
        "title": "Integer echo",
        "domain": "testing",
        "task_sources": [],
        "metric": {"description": "One is correct and zero is incorrect.", "type": None},
        "baseline": {
            "existing_baseline": True,
            "name": "Copy the public integer",
            "source": "https://example.invalid/baselines/copy-input",
            "score": 1.0,
            "bottom_line_score": 0.0,
        },
    }


def _verifier_source() -> str:
    return """\
import argparse
import json
from pathlib import Path
from science_innovation_exam.verify_context import VerifyContext

parser = argparse.ArgumentParser()
parser.add_argument('--input', required=True)
parser.add_argument('--submission', required=True)
parser.add_argument('--result', required=True)
args = parser.parse_args()
expected = json.loads((Path(args.input) / 'value.json').read_text())['value']
observed = json.loads((Path(args.submission) / 'submission.json').read_text())['value']
value = 1.0 if expected == observed else 0.0
validation_count = VerifyContext.current().get('validation_count', 0)
VerifyContext.current().log({'phase': 'test', 'private_score': value})
result = {
    'schema_version': '1.0',
    'task_id': 'integer_echo',
    'valid': True,
    'status': 'valid',
    'primary_metric': {'name': 'accuracy', 'value': value, 'direction': 'maximize'},
    'metrics': [
        {'name': 'accuracy', 'value': value, 'unit': None},
        {'name': 'validation_count', 'value': validation_count, 'unit': 'calls'},
    ],
    'error': None,
}
Path(args.result).write_text(json.dumps(result, sort_keys=True) + '\\n')
"""


def _validation_source() -> str:
    return """\
import argparse
import json
from pathlib import Path
from science_innovation_exam.verify_context import VerifyContext

parser = argparse.ArgumentParser()
parser.add_argument('--input', required=True)
parser.add_argument('--request', required=True)
parser.add_argument('--response', required=True)
args = parser.parse_args()
request = json.loads(Path(args.request).read_text())
context = VerifyContext.current()
count = context.get('validation_count', 0) + 1
context['validation_count'] = count
seen = context.get('seen', [])
seen.append(request['value'])
context['seen'] = seen
context.log({'phase': 'validation', 'round': count, 'private_note': 'diagnostic'})
Path(args.response).write_text(json.dumps({'count': count, 'seen': seen}, sort_keys=True) + '\\n')
"""


def _make_package(root: Path) -> Path:
    package = root / "integer_echo"
    (package / "input").mkdir(parents=True)
    (package / "environment").mkdir()
    (package / "verifier" / "test" / "run").mkdir(parents=True)
    (package / "verifier" / "validation" / "run").mkdir(parents=True)
    _write_json(package / "instruction.json", _instruction())
    _write_json(package / "meta.json", _meta())
    _write_json(
        package / "environment" / "environment.json",
        {"schema_version": "1.0", "type": "default"},
    )
    _write_json(package / "input" / "value.json", {"value": 7})
    (package / "verifier" / "test" / "run" / "main.py").write_text(
        _verifier_source(), encoding="utf-8"
    )
    (package / "verifier" / "validation" / "run" / "main.py").write_text(
        _validation_source(), encoding="utf-8"
    )
    return package
