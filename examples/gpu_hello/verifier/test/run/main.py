"""CPU-only deterministic output check; deliberately no hardware attestation."""
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--input', type=Path, required=True)
parser.add_argument('--submission', type=Path, required=True)
parser.add_argument('--result', type=Path, required=True)
args = parser.parse_args()
try:
    data = json.loads((args.input / 'data.json').read_text())
    output = json.loads((args.submission / 'result.json').read_text())
    expected = [a + b for a, b in zip(data['a'], data['b'])]
    valid = output.get('backend') == 'cuda' and isinstance(output.get('device'), str) and \
        bool(output['device']) and output.get('values') == expected
except (ValueError, OSError, TypeError, AttributeError):
    valid = False
args.result.write_text(json.dumps({
    'schema_version': '1.0', 'task_id': 'gpu_hello', 'valid': valid,
    'status': 'valid' if valid else 'invalid',
    'primary_metric': {'name': 'correct_output', 'value': 1.0 if valid else 0.0, 'direction': 'maximize'},
    'metrics': [{'name': 'correct_output', 'value': 1.0 if valid else 0.0, 'unit': None}],
    'error': None if valid else 'Expected CUDA vector sum.'
}) + '\n')
