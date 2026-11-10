"""Private-directory, atomic-write and file-lock helpers (ported from legacy)."""

import fcntl
import hashlib
import json
import os
import tempfile
from pathlib import Path

from .constants import MAX_FILE_BYTES
from .errors import AuthError


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True,
                                     separators=(',', ':'),
                                     ensure_ascii=False).encode()).hexdigest()


def ensure_private_directory(directory: Path) -> None:
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        mode = directory.stat().st_mode & 0o777
        if mode & 0o077:
            os.chmod(directory, mode & 0o700)
    except OSError:
        raise AuthError('oauth_local_write_failed') from None


def read_private_json(path: Path):
    """Read a private JSON file; returns None when missing."""
    try:
        if not path.exists():
            return None
        if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
            raise AuthError('oauth_invalid_local_file')
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as source:
            raw = source.read(MAX_FILE_BYTES + 1)
        if len(raw) > MAX_FILE_BYTES:
            raise AuthError('oauth_invalid_local_file')
        return json.loads(raw)
    except FileNotFoundError:
        return None
    except OSError:
        raise AuthError('oauth_invalid_local_file') from None
    except (ValueError, UnicodeDecodeError):
        raise AuthError('oauth_invalid_local_file') from None


def write_private_json(path: Path, value) -> None:
    """Atomic durable write: temp file + fsync + rename + directory fsync."""
    ensure_private_directory(path.parent)
    raw = (json.dumps(value, separators=(',', ':')) + '\n').encode()
    if len(raw) > MAX_FILE_BYTES:
        raise AuthError('oauth_invalid_response')
    temp_path = None
    try:
        fd, temp_path = tempfile.mkstemp(prefix='.auth-', dir=path.parent)
        with os.fdopen(fd, 'wb') as target:
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temp_path, path)
        temp_path = None
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except OSError:
        raise AuthError('oauth_local_write_failed') from None
    finally:
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError:
                pass


class FileLock:
    """Private, non-blocking process lock (fcntl; mac/linux)."""

    def __init__(self, path: Path):
        self.path = path
        self.fd = None

    def __enter__(self):
        ensure_private_directory(self.path.parent)
        fd = os.open(self.path,
                     os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise AuthError('oauth_busy') from None
        except OSError:
            os.close(fd)
            raise AuthError('oauth_invalid_local_file') from None
        self.fd = fd
        return self

    def __exit__(self, *args):
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None
