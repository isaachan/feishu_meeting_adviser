"""Log file tailing for the admin-site streaming face (tail -f semantics).

The follower holds no threads: the HTTP layer polls ``read_new()`` and gets
whatever was appended since the previous call. The first successful call
bootstraps from the tail of the file (bounded by ``bootstrap_bytes``);
rotation (new inode) and in-place truncation are detected and reported via a
marker line so the UI can show the discontinuity.
"""
from pathlib import Path
from typing import IO
import os

TRUNCATED_MARKER = '…（前文已截断，仅显示末尾）…'
ROTATED_MARKER = '…（日志文件被轮转/重建，已重新跟踪）…'


class LogFollower:
    """Tail one file; ``read_new()`` returns text appended since the last call."""

    def __init__(self, path, *, bootstrap_bytes: int = 65536):
        self.path = Path(path)
        self.bootstrap_bytes = bootstrap_bytes
        self._fh: IO[bytes] | None = None
        self._inode: tuple[int, int] | None = None
        self._offset = 0

    def close(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None

    def _path_inode(self):
        try:
            st = self.path.stat()
        except OSError:
            return None
        return (st.st_dev, st.st_ino)

    def _attach(self, fh) -> None:
        if self._fh is not None:
            self._fh.close()
        self._fh = fh
        st = os.fstat(fh.fileno())
        self._inode = (st.st_dev, st.st_ino)
        self._offset = 0

    def _read_tail(self) -> str:
        """Bounded read of the file tail; positions the offset at EOF."""
        fh = self._fh
        assert fh is not None
        st = os.fstat(fh.fileno())
        start = max(0, st.st_size - self.bootstrap_bytes)
        fh.seek(start)
        data = fh.read()
        self._offset = start + len(data)
        prefix = f'{TRUNCATED_MARKER}\n' if start > 0 else ''
        return prefix + data.decode('utf-8', 'replace')

    def _read_appended(self) -> str:
        fh = self._fh
        assert fh is not None
        data = fh.read()
        if not data:
            return ''
        self._offset += len(data)
        return data.decode('utf-8', 'replace')

    def read_new(self) -> str:
        if self._fh is None:
            if self._path_inode() is None:
                return ''
            try:
                fh = open(self.path, 'rb')
            except OSError:
                return ''
            self._attach(fh)
            return self._read_tail()

        inode = self._path_inode()
        if inode is not None and inode != self._inode:
            try:
                fh = open(self.path, 'rb')
            except OSError:
                return ''
            self._attach(fh)
            return f'{ROTATED_MARKER}\n' + self._read_tail()

        try:
            st = os.fstat(self._fh.fileno())
        except OSError:
            self.close()
            return ''
        if st.st_size < self._offset:  # rewritten in place
            self._fh.seek(0)
            self._offset = 0
            return f'{ROTATED_MARKER}\n' + self._read_tail()
        return self._read_appended()
