"""Credential-free source identity and runtime evidence; no network requests."""
import argparse
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import inspect
import os
from pathlib import Path
import platform
import shutil
import sys

from .bounded_output import run as capture
from .common.bounded_io import read_bytes
from .common.serialization import atomic_write_json


def source_record(path, timeout=5, maximum_bytes=1048576):
    """Store identity/status only, never Git remotes, diffs or untracked contents."""
    source = Path(path).resolve()
    directory = source if source.is_dir() else source.parent
    record = {'source': str(source), 'commit': None, 'dirty': None}
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
    env['GIT_OPTIONAL_LOCKS'] = '0'
    def git(*args):
        result = capture(['git', '-c', 'core.fsmonitor=false', '-C', str(directory)] + list(args),
                         maximum_bytes, timeout=timeout, env=env)
        if result.returncode:
            raise ValueError('git metadata unavailable')
        return result.stdout
    try:
        record['repository'] = git('rev-parse', '--show-toplevel').decode().strip()
        record['commit'] = git('rev-parse', 'HEAD').decode().strip()
        relative = '.' if source.is_dir() else source.name  # git -C already selects the source directory.
        record['source_tracked'] = bool(git('ls-files', '-z', '--', relative))
        if not record['source_tracked']:
            # An ignored task copy inside a checkout is not versioned by its
            # parent's commit. The task's content fingerprint still identifies it.
            record['repository_commit'], record['commit'] = record['commit'], None
        status = git('status', '--porcelain=v1', '-z', '--untracked-files=normal')
        # Rename entries have a second NUL-delimited pathname. Only the first
        # field of each entry is a status; filenames themselves remain private.
        entries, index, tracked, untracked = status.split(b'\0'), 0, 0, 0
        while index < len(entries) and entries[index]:
            code = entries[index][:2]
            untracked += int(code == b'??')
            tracked += int(code != b'??')
            index += 2 if b'R' in code or b'C' in code else 1
        record.update(dirty=bool(status), tracked_changes=tracked, untracked_entries=untracked,
                      status_sha256=hashlib.sha256(status).hexdigest())
    except Exception as error:
        record['observation_error'] = type(error).__name__
    return record


def environment_record(timeout=5, maximum_bytes=1048576):
    record = {'python': platform.python_version(), 'python_implementation': platform.python_implementation(),
              'system': platform.system(), 'kernel': platform.release(), 'architecture': platform.machine(),
              'logical_cpus_visible': os.cpu_count()}
    record['packages'] = {}
    for name in ('science-innovation-exam', 'PyYAML', 'jsonschema', 'referencing', 'numpy', 'torch'):
        try:
            record['packages'][name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            record['packages'][name] = None
    try:
        record['cpu_affinity_count'] = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        record['cpu_affinity_count'] = None
    try:
        for line in read_bytes('/proc/meminfo', maximum_bytes).decode().splitlines():
            if line.startswith('MemTotal:'):
                record['system_memory_bytes'] = int(line.split()[1]) * 1024
                break
    except (OSError, ValueError):
        record['system_memory_bytes'] = None
    record['cgroup'] = {}
    for name in ('cpu.max', 'memory.max', 'pids.max'):
        try:
            record['cgroup'][name] = read_bytes(Path('/sys/fs/cgroup') / name, 256).decode().strip()
        except (OSError, ValueError):
            record['cgroup'][name] = None
    nvidia = shutil.which('nvidia-smi')
    record['gpu_inventory'] = {'available': False, 'reason': 'nvidia_smi_not_installed'}
    if nvidia:
        try:
            result = capture([nvidia, '--query-gpu=name,uuid,driver_version,memory.total',
                              '--format=csv,noheader,nounits'], maximum_bytes, timeout=timeout)
            record['gpu_inventory'] = ({'available': True, 'csv': result.stdout.decode().strip()}
                                       if result.returncode == 0 else {'available': False, 'reason': 'query_failed'})
        except Exception as error:
            record['gpu_inventory'] = {'available': False, 'reason': type(error).__name__}
    return record


def backend_record(service, options):
    record = source_record(inspect.getfile(type(service)), options['timeout_seconds'], options['maximum_bytes'])
    record['module'] = type(service).__module__
    return record


def collect(config, task):
    options = config['provenance']
    timeout, maximum = options['timeout_seconds'], options['maximum_bytes']
    import science_innovation_exam
    record = {'schema_version': '1.0', 'recorded_at': datetime.now(timezone.utc).isoformat(),
              'framework': source_record(science_innovation_exam.__file__, timeout, maximum),
              'task': source_record(task.root, timeout, maximum),
              'controller': environment_record(timeout, maximum),
              'configured_resources': {'agent': config['resources'], 'verifier': config['verifier']},
              'agent': {'type': config['agent']['type'], 'model': config['agent'].get('model'),
                        'reasoning_effort': config['agent'].get('reasoning_effort')}}
    record['task']['fingerprint'] = task.fingerprint()
    return record


def command(argv, output, options):
    return ['python3', '-B', '-m', 'science_innovation_exam.provenance', '--output', output,
            '--timeout-seconds', str(options['timeout_seconds']), '--maximum-bytes', str(options['maximum_bytes']),
            '--'] + list(argv)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    parser.add_argument('--timeout-seconds', type=float, default=5)
    parser.add_argument('--maximum-bytes', type=int, default=1048576)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    argv = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not argv or args.timeout_seconds <= 0 or args.maximum_bytes <= 0:
        parser.error('positive limits and a command are required')
    try:
        atomic_write_json(Path(args.output), environment_record(args.timeout_seconds, args.maximum_bytes))
    except Exception as error:
        # Diagnostic capture must not prevent an otherwise runnable experiment.
        print('runtime evidence unavailable: ' + type(error).__name__, file=sys.stderr)
    os.execvp(argv[0], argv)


if __name__ == '__main__':
    main()
