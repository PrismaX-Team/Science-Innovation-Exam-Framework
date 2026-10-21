import argparse
import json
from pathlib import Path

from .common.errors import ScienceInnovationExamError
from .runner import run
from .recovery import cleanup_run
from .task import TaskPackage


def main(argv=None):
    parser = argparse.ArgumentParser(prog="sie")
    commands = parser.add_subparsers(dest="command", required=True)
    execute = commands.add_parser("run", help="run one experiment using the configured container backend")
    execute.add_argument("--config", required=True, type=Path)
    check = commands.add_parser("check", help="check a five-part task package")
    check.add_argument("task", type=Path)
    show = commands.add_parser("show", help="read one experiment result")
    show.add_argument("run_directory", type=Path)
    cleanup = commands.add_parser('cleanup', help='reclaim this run after its controller has exited')
    cleanup.add_argument('--run', required=True, type=Path, dest='run_directory')
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            result = run(args.config)
        elif args.command == "check":
            task = TaskPackage.load(args.task)
            result = {"task_id": task.task_id, "submission_paths": task.submission_paths, "status": "package_valid"}
        elif args.command == 'cleanup':
            result = cleanup_run(args.run_directory)
        else:
            result = json.loads((args.run_directory / "result.json").read_text())
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] in ("scored", "package_valid", "cleaned") else 1
    except (ScienceInnovationExamError, ValueError, OSError) as error:
        parser.exit(2, "%s: %s\n" % (type(error).__name__, error))
