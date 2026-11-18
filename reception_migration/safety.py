"""Restrict every database operation to an owned Python temporary workspace.

This is an accident-prevention boundary, not a sandbox against hostile code running
as the same Unix user. No caller-supplied arbitrary directory is accepted.
"""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
from uuid import uuid4


TEMP_ROOT = Path('/tmp')
PREFIX = 'omachi-migration-'
MARKER = '.synthetic-workspace.json'
PROJECT = Path(__file__).resolve().parents[1]
PROTECTED_FILES = (
    PROJECT / 'var/receptions.sqlite3', PROJECT / 'var/dev-receptions.sqlite3',
    Path.home() / 'omachi-do-sandbox/seat-correction-test-20261029-124134-293403.sqlite3',
)
PROTECTED_DIRECTORIES = (Path.home() / 'omachi-do-backups',)


class SafetyError(ValueError):
    """A path is not an authorized synthetic temporary database."""


def _regular(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid():
        raise SafetyError('Only owned regular files with one hard link are allowed.')
    return info


@contextmanager
def _opened_regular(path, flags):
    before = _regular(path)
    fd = os.open(path, flags | os.O_NOFOLLOW)
    try:
        opened = os.fstat(fd)
        if (not os.path.samestat(before, opened) or opened.st_nlink != 1
                or not os.path.samestat(opened, path.lstat())):
            raise SafetyError('File changed while opening.')
        yield fd
    finally:
        os.close(fd)


def _manifest(fd):
    os.lseek(fd, 0, os.SEEK_SET)
    with os.fdopen(os.dup(fd), 'r') as stream:
        value = json.load(stream)
    try:
        if (value['purpose'] != 'omachi-synthetic-v1-v2' or value['version'] != 1
                or not isinstance(value['databases'], dict)):
            raise ValueError
        from uuid import UUID
        UUID(value['workspace_id'])
        for name, checksum in value['databases'].items():
            if (Path(name).name != name or not name.endswith('.sqlite3')
                    or not isinstance(checksum, str) or len(checksum) != 64
                    or any(char not in '0123456789abcdef' for char in checksum)):
                raise ValueError
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise SafetyError('Missing synthetic workspace provenance.') from exc
    return value


class Workspace:
    def __init__(self, root):
        self.root = Path(root).absolute()
        if self.root.parent != TEMP_ROOT or not self.root.name.startswith(PREFIX):
            raise SafetyError('Use temporary_workspace(); arbitrary directories are forbidden.')
        info = self.root.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise SafetyError('Workspace must be an owned private temporary directory.')
        self._identity = (info.st_dev, info.st_ino)
        with _opened_regular(self.root / MARKER, os.O_RDONLY) as fd:
            self.manifest = _manifest(fd)
        self._workspace_id = self.manifest['workspace_id']

    def _recheck(self):
        checked = Workspace(self.root)
        if checked._identity != self._identity or checked._workspace_id != self._workspace_id:
            raise SafetyError('Temporary workspace identity changed.')
        self.manifest = checked.manifest
        return checked

    def path(self, name):
        value = Path(name)
        path = value if value.is_absolute() else self.root / value
        # Direct children only: no directory symlinks, traversal, or alias paths.
        if path.parent != self.root or not path.name.endswith('.sqlite3'):
            raise SafetyError('Database must be a direct child of the temporary workspace.')
        if path in PROTECTED_FILES or any(path.is_relative_to(p) for p in PROTECTED_DIRECTORIES):
            raise SafetyError('Protected database or backup.')
        return path

    def new_file(self, name):
        self._recheck()
        path = self.path(name)
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        return path

    def authorize_synthetic(self, name):
        self._recheck()
        path = self.path(name)
        # Called by the synthetic fixture generator or a copy within this workspace.
        with _opened_regular(path, os.O_RDONLY) as source_fd:
            before = os.fstat(source_fd)
            with os.fdopen(os.dup(source_fd), 'rb') as stream:
                checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
            after = os.fstat(source_fd)
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                    after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise SafetyError('Synthetic database changed while hashing.')
        with _opened_regular(self.root / MARKER, os.O_RDWR) as fd:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            manifest = _manifest(fd)
            if manifest['workspace_id'] != self._workspace_id:
                raise SafetyError('Temporary workspace identity changed.')
            manifest['databases'][path.name] = checksum
            payload = json.dumps(manifest, sort_keys=True).encode()
            os.lseek(fd, 0, os.SEEK_SET)
            os.ftruncate(fd, 0)
            with os.fdopen(os.dup(fd), 'wb') as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            self.manifest = manifest

    @contextmanager
    def connection(self, name, *, recovery=False, readonly=False):
        # Revalidate the root and marker on every operation, including subprocesses.
        checked = self._recheck()
        path = checked.path(name)
        if path.name not in checked.manifest['databases']:
            raise SafetyError('Database was not generated or copied by this synthetic workspace.')
        before = _regular(path)
        for protected in PROTECTED_FILES:
            if protected.exists() and os.path.samestat(before, protected.stat()):
                raise SafetyError('Protected database inode.')
        for suffix in ('-journal', '-wal', '-shm'):
            sidecar = Path(str(path) + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                _regular(sidecar)
                if not recovery:
                    raise SafetyError('Active/recovery sidecar present; migration refused.')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        conn = None
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if not os.path.samestat(before, os.fstat(fd)):
                raise SafetyError('Database changed while opening.')
            header = os.read(fd, 100)
            if header.startswith(b'SQLite format 3\0') and header[18:20] != b'\x01\x01':
                raise SafetyError('The synthetic migration lab accepts rollback-journal databases only.')
            uri = path.as_uri() + ('?mode=ro' if readonly else '?mode=rw')
            conn = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=1)
            if not os.path.samestat(before, path.lstat()):
                raise SafetyError('Database path changed while connecting.')
            conn.execute('PRAGMA foreign_keys=ON')
            if conn.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
                raise SafetyError('Foreign-key enforcement unavailable.')
            yield conn
        finally:
            if conn is not None:
                if conn.in_transaction:
                    conn.rollback()
                conn.close()
            os.close(fd)


@contextmanager
def temporary_workspace():
    with tempfile.TemporaryDirectory(prefix=PREFIX, dir=TEMP_ROOT) as directory:
        root = Path(directory)
        marker = root / MARKER
        fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w') as stream:
            json.dump({'purpose': 'omachi-synthetic-v1-v2', 'version': 1,
                       'workspace_id': str(uuid4()), 'databases': {}}, stream)
        yield Workspace(root)
