#!/usr/bin/env python3
"""Public infrastructure self-check, using only public input and actions."""
import argparse
import json
from pathlib import Path
import subprocess

parser = argparse.ArgumentParser()
parser.add_argument('--input', type=Path, required=True)
parser.add_argument('--submission', type=Path, required=True)
args = parser.parse_args()
value = json.loads((args.input / 'value.json').read_text())
request = Path('/tmp/echo-validation.json')
request.write_text(json.dumps(value))
reply = subprocess.run(
    ['sie-action', 'validation', '--request', str(request)],
    capture_output=True, text=True, check=True,
)
assert json.loads(reply.stdout)['response'] == {'count': 1, 'seen': [value['value']]}
(args.submission / 'submission.json').write_text(json.dumps(value) + '\n')
subprocess.run(['sie-action', 'test'], check=True)
