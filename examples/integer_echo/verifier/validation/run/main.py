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
Path(args.response).write_text(json.dumps({'count': count, 'seen': seen}, sort_keys=True) + '\n')
