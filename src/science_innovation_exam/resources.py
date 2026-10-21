"""Durable resource intents and exclusive controller/reclaimer ownership (Linux)."""
import fcntl
import json
import os
from pathlib import Path
import socket
import stat
import threading
import uuid
from datetime import datetime, timezone

from .common.errors import InfrastructureError
from .common.serialization import atomic_write_json


def read_record(path):
    fd = os.open(str(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as source:
        if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
            raise InfrastructureError('recovery record must be a regular file')
        data = source.read(4 * 1024 * 1024 + 1)
    if len(data) > 4 * 1024 * 1024:
        raise InfrastructureError('recovery record exceeds size limit')
    return json.loads(data)


class RunLock:
    """Never expire/steal a live lock; SIGKILL and reboot release kernel locks.

    Requires a filesystem with coherent flock semantics. Only the originating
    host may reclaim; no PID-based killing and no unsafe force override.
    """
    def __init__(self, root, operation='run'):
        self.root, self.operation = Path(root), operation
        self.fd = None

    def __enter__(self):
        fd = os.open(str(self.root / 'controller.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                raise InfrastructureError('controller lock must be a regular file')
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            path = self.root / 'controller.json'
            if path.is_symlink():
                raise InfrastructureError('controller record cannot be a symbolic link')
            if path.exists():
                old = read_record(path)
                if old['host'] != socket.gethostname():
                    raise InfrastructureError('cleanup must run on the originating controller host')
            self.record = {'host': socket.gethostname(), 'pid': os.getpid(),
                           'operation': self.operation, 'state': 'owned',
                           'acquired_at': datetime.now(timezone.utc).isoformat()}
            atomic_write_json(path, self.record)
        except BlockingIOError:
            os.close(fd)
            raise InfrastructureError('run is still owned by a controller or another cleanup process') from None
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd
        return self

    def __exit__(self, *_args):
        try:
            atomic_write_json(self.root / 'controller.json', dict(self.record, state='idle'))
        finally:
            os.close(self.fd)
            self.fd = None


class ResourceJournal:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.path = self.root / 'resources.json'
        self.lock = threading.RLock()
        self.data = read_record(self.path)
        if (self.data.get('schema_version') != '1.0' or self.data.get('run_id') != self.root.name
                or self.data.get('run_directory') != str(self.root)
                or not isinstance(self.data.get('owner'), str) or len(self.data['owner']) != 32
                or not isinstance(self.data.get('resources'), dict)):
            raise InfrastructureError('invalid or relocated resource journal')

    @classmethod
    def create(cls, root, backend):
        root = Path(root).resolve()
        path = root / 'resources.json'
        if path.exists() or path.is_symlink():
            raise InfrastructureError('resource journal already exists')
        atomic_write_json(path, {'schema_version': '1.0', 'run_id': root.name,
                                'run_directory': str(root), 'owner': uuid.uuid4().hex,
                                'backend': backend, 'bindings': {}, 'resources': {}})
        return cls(root)

    @property
    def owner(self):
        return self.data['owner']

    def save(self):
        atomic_write_json(self.path, self.data)

    def bind(self, key, value):
        with self.lock:
            prior = self.data['bindings'].get(key)
            if prior is not None and prior != value:
                raise InfrastructureError('resource backend scope changed: ' + key)
            self.data['bindings'][key] = value
            self.save()

    def intent(self, key, kind, identity):
        with self.lock:
            old = self.data['resources'].get(key)
            if old and old['state'] != 'removed':
                raise InfrastructureError('unresolved resource already exists: ' + key)
            entry = {'kind': kind, 'identity': identity, 'state': 'intent',
                     'request': uuid.uuid4().hex, 'id': None}
            self.data['resources'][key] = entry
            self.save()  # Must succeed before any create request or local mkdir.
            return entry

    def update(self, key, **fields):
        with self.lock:
            self.data['resources'][key].update(fields)
            self.save()

    def pending(self, kinds=None):
        with self.lock:
            return [(key, json.loads(json.dumps(value))) for key, value in self.data['resources'].items()
                    if value['state'] != 'removed' and (kinds is None or value['kind'] in kinds)]

    def removed(self, key):
        self.update(key, state='removed', error=None)

    def forget(self, key, expected_kind):
        with self.lock:
            if self.data['resources'][key]['kind'] != expected_kind:
                raise InfrastructureError('resource kind does not match completed transient operation')
            del self.data['resources'][key]
            self.save()
