"""Minimal private-path and lock utilities (gateway-local copies).

Duplicated from the adviser module on purpose: the gateway never imports
client-side code (plan section 3.4).
"""
import fcntl
import os
from pathlib import Path
import stat


class LockBusy(Exception):
    pass


class LockError(Exception):
    pass


def private_directory(path: Path) -> None:
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise LockError('runtime directory must be real with mode 700')


class FileLock:
    """Private, non-blocking process file lock."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.fd = None

    def acquire(self):
        private_directory(self.path.parent)
        if self.path.is_symlink() or self.path.exists() and self.path.stat().st_mode & 0o077:
            raise LockError('invalid lock file permissions')
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise LockBusy('operation already active') from None
        except OSError:
            os.close(fd)
            raise LockError('unable to acquire lock') from None
        self.fd = fd

    def release(self):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *args):
        self.release()
