import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument('--input', required=True)
parser.add_argument('--request', type=Path, required=True)
parser.add_argument('--response', type=Path, required=True)
args = parser.parse_args()
json.loads(args.request.read_text())
args.response.write_text(json.dumps({'message': 'Return result.json from GPU and call sie-action test on the agent machine.'}) + '\n')
