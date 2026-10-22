"""Stdlib-only bounded subprocess capture, also available inside task images."""
import argparse
import os
from pathlib import Path
import selectors
import stat
import subprocess
import time

from .common.serialization import atomic_write_json


class OutputLimitError(ValueError):
    pass


def run(argv, maximum_bytes, timeout=None, input=None, env=None, directory=None):
    """Drain both pipes without buffering unlimited output or breaking the child.

    File capture clips each stream and preserves the command exit code. Memory
    capture rejects oversized control responses instead of parsing a prefix.
    """
    process = subprocess.Popen(argv, stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)
    outputs = [bytearray(), bytearray()]
    sizes, clipped, files = [0, 0], [False, False], []
    deadline = None if timeout is None else time.monotonic() + timeout
    try:
        if directory is not None:
            directory = Path(directory)
            directory.mkdir(parents=True, exist_ok=True)
            for name in ('stdout.log', 'stderr.log'):
                fd = os.open(str(directory / name), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
                handle = os.fdopen(fd, 'wb')
                files.append(handle)
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise OSError('output log must be a regular file')
                handle.truncate(0)
        with selectors.DefaultSelector() as selector:
            for index, stream in enumerate((process.stdout, process.stderr)):
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, index)
            pending = memoryview(input) if input is not None else None
            if pending is not None:
                os.set_blocking(process.stdin.fileno(), False)
                selector.register(process.stdin, selectors.EVENT_WRITE, 2)
            while selector.get_map():
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout)
                for key, _events in selector.select(remaining):
                    stream, index = key.fileobj, key.data
                    if index == 2:
                        try:
                            sent = os.write(stream.fileno(), pending[:65536]) if pending else 0
                            pending = pending[sent:]
                        except BrokenPipeError:
                            pending = pending[:0]
                        if not pending:
                            selector.unregister(stream)
                            stream.close()
                        continue
                    block = os.read(stream.fileno(), 65536)
                    if not block:
                        selector.unregister(stream)
                        stream.close()
                        continue
                    selected = block[:max(0, maximum_bytes - sizes[index])]
                    clipped[index] |= len(selected) != len(block)
                    sizes[index] += len(selected)
                    if files:
                        files[index].write(selected)
                        files[index].flush()
                    else:
                        outputs[index].extend(selected)
            code = process.wait(timeout=None if deadline is None else max(0, deadline - time.monotonic()))
        if directory is None and any(clipped):
            raise OutputLimitError('subprocess response exceeds %d bytes per stream' % maximum_bytes)
        if directory is not None:
            atomic_write_json(directory / 'capture.json', {
                'maximum_bytes_per_stream': maximum_bytes, 'stdout_truncated': clipped[0],
                'stderr_truncated': clipped[1], 'exit_code': code})
        return subprocess.CompletedProcess(argv, code, bytes(outputs[0]), bytes(outputs[1]))
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()
        for handle in files:
            handle.close()


def command(argv, directory, maximum_bytes):
    return ['python3', '-B', '-m', 'science_innovation_exam.bounded_output',
            '--directory', directory, '--maximum-bytes', str(maximum_bytes), '--'] + list(argv)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--directory', required=True)
    parser.add_argument('--maximum-bytes', type=int, required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    argv = args.command[1:] if args.command[:1] == ['--'] else args.command
    if args.maximum_bytes <= 0 or not argv:
        parser.error('a positive limit and command are required')
    code = run(argv, args.maximum_bytes, directory=args.directory).returncode
    return 128 - code if code < 0 else code


if __name__ == '__main__':
    raise SystemExit(main())
