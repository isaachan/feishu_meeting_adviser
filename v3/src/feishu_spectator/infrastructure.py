"""Local infrastructure: private directories, advisory locks, shared errors."""
from contextlib import contextmanager
import fcntl
import os
from pathlib import Path


class LifecycleError(Exception):
    """Operator-actionable local state error; never carries server payloads."""


class LockBusy(Exception):
    pass


def private_directory(path: Path) -> None:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or not path.is_dir():
        raise LifecycleError('private storage path must be a real directory')
    os.chmod(path, 0o700)


def _check_private_file(path: Path) -> None:
    if path.is_symlink() or (path.exists() and (not path.is_file() or path.stat().st_mode & 0o077)):
        raise LifecycleError('private file must be a regular local file with mode 600')


def read_private_json(path: Path, *, limit: int) -> dict:
    _check_private_file(path)
    if not path.exists():
        raise FileNotFoundError(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as source:
        raw = source.read(limit + 1)
    import json
    if len(raw) > limit:
        raise LifecycleError('private file exceeds expected size')
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise LifecycleError('private file must hold an object')
    return value


def write_private_json(path: Path, value: dict, *, limit: int) -> None:
    import json
    import tempfile
    _check_private_file(path)
    raw = (json.dumps(value, separators=(',', ':')) + '\n').encode()
    if len(raw) > limit:
        raise LifecycleError('private file exceeds expected size')
    temp_path = None
    try:
        fd, temp_path = tempfile.mkstemp(prefix='.private-', dir=path.parent)
        with os.fdopen(fd, 'wb') as target:
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
        temp_path = None
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        raise LifecycleError('private file write failed') from None
    finally:
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


class FileLock:
    """Blocking advisory flock with context-manager use; LockBusy on contention."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fd = None

    def acquire(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self._fd)
            self._fd = None
            raise LockBusy('lock held by another process: ' + self.path.name) from None

    def release(self):
        if self._fd is not None:
            fd, self._fd = self._fd, None
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def is_locked(self) -> bool:
        try:
            self.acquire()
        except LockBusy:
            return True
        self.release()
        return False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc_info):
        self.release()
