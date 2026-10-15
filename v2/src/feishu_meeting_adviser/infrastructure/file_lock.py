"""Private, non-blocking process file locks."""
import fcntl
import os
from pathlib import Path

from .errors import LifecycleError, LockBusy
from ..adapters.persistence.sqlite import private_directory


class FileLock:
    def __init__(self, path: Path):
        self.path = path
        self.fd = None

    def acquire(self):
        private_directory(self.path.parent)
        if self.path.is_symlink() or self.path.exists() and self.path.stat().st_mode & 0o077:
            raise LifecycleError('invalid lock file permissions')
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise LockBusy('operation or collector already active; stop it first') from None
        except OSError:
            os.close(fd)
            raise LifecycleError('unable to acquire local operation lock') from None
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
