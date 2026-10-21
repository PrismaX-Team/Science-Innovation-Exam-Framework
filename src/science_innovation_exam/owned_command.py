"""Linux parent-death guard for a mutating transfer command; no background daemon."""
import ctypes
import os
from pathlib import Path
import signal
import sys


def process_identity(pid):
    try:
        fields = Path('/proc/%d/stat' % pid).read_text().rsplit(')', 1)[1].split()
        return {'pid': pid, 'start_ticks': fields[19], 'state': fields[0],
                'boot': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
    except FileNotFoundError:
        return None


def still_running(identity):
    current = process_identity(identity['pid'])
    return (current is not None and current['state'] != 'Z'
            and all(current[key] == identity[key] for key in ('pid', 'start_ticks', 'boot')))


def main():
    # This executes after Python starts in the child, not preexec_fn in a threaded parent.
    parent, record = int(sys.argv[1]), Path(sys.argv[2])
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGKILL, 0, 0, 0):  # PR_SET_PDEATHSIG, Linux
        raise OSError(ctypes.get_errno(), 'cannot establish parent-death guard')
    if os.getppid() != parent:
        return 1  # The parent may have died before the guard was installed.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from science_innovation_exam.common.serialization import atomic_write_json
    atomic_write_json(record, process_identity(os.getpid()))  # Durable before executing any write.
    os.execvp(sys.argv[3], sys.argv[3:])


if __name__ == '__main__':
    raise SystemExit(main())
