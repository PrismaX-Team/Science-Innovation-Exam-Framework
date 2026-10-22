"""Read untrusted regular files with a byte cap, without following final links."""
import os
import re
import stat
import tempfile
from pathlib import Path


def read_bytes(path, maximum_bytes):
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ValueError('input must be a regular file')
        data = handle.read(maximum_bytes + 1)
    if len(data) > maximum_bytes:
        raise ValueError('input exceeds %d bytes' % maximum_bytes)
    return data


def redact_file(path, secrets, maximum_bytes):
    """Bounded-memory, atomic redaction, including secrets across block boundaries.

    Read a small overlap beyond the retained prefix so truncation cannot leave
    the beginning of a secret whose end is just outside the log budget.
    """
    path = Path(path)
    tokens = sorted({s.encode() for s in secrets if s}, key=len, reverse=True)
    overlap = max((len(s) for s in tokens), default=1) - 1
    pattern = re.compile(b'|'.join(re.escape(s) for s in tokens)) if tokens else None
    source_fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    temporary = None
    try:
        with os.fdopen(source_fd, 'rb') as source:
            identity = os.fstat(source.fileno())
            if not stat.S_ISREG(identity.st_mode):
                raise ValueError('log must be a regular file')
            descriptor, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=str(path.parent))
            temporary = Path(name)
            os.fchmod(descriptor, stat.S_IMODE(identity.st_mode))
            with os.fdopen(descriptor, 'wb') as output:
                pending, remaining, written = b'', maximum_bytes + overlap, 0
                while True:
                    block = source.read(min(65536, remaining)) if remaining else b''
                    remaining -= len(block)
                    data = pending + block
                    cutoff = max(0, len(data) - overlap) if block else len(data)
                    cursor, parts = 0, []
                    if pattern is not None:
                        for match in pattern.finditer(data):
                            if match.start() >= cutoff:
                                break
                            parts.extend((data[cursor:match.start()], b'[REDACTED]'))
                            cursor = match.end()
                    cutoff = max(cursor, cutoff)
                    parts.append(data[cursor:cutoff])
                    selected = b''.join(parts)[:max(0, maximum_bytes - written)]
                    output.write(selected)
                    written += len(selected)
                    pending = data[cutoff:]
                    if not block:
                        break
                output.flush()
                os.fsync(output.fileno())
            current = path.lstat()
            if (current.st_dev, current.st_ino) != (identity.st_dev, identity.st_ino):
                raise OSError('log changed during redaction')
            os.replace(str(temporary), str(path))
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
