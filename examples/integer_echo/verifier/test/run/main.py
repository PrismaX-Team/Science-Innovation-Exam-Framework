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
Path(args.result).write_text(json.dumps(result, sort_keys=True) + '\n')
