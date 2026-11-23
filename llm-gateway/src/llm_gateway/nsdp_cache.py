"""Owner-scoped, cross-process cache for successful NSDP release lookups."""
import hashlib
import hmac
import os
from pathlib import Path
import sqlite3
import stat
import time

from ._private import FileLock, LockBusy, private_directory
from .nsdp_query import QueryError


class OwnerReleaseCache:
    TTL_S = 3600
    MAX_OWNER_ENTRIES = 64
    MAX_OWNER_BYTES = 2_000_000

    def __init__(self, path: Path, *, clock=time.time):
        self.path = Path(path)
        private_directory(self.path.parent)
        if self.path.is_symlink():
            raise OSError('cache database must not be a symlink')
        self.clock = clock
        self.key, created = self._key(self.path.with_name(self.path.name + '.hmac'))
        self.db = sqlite3.connect(str(self.path), isolation_level=None, timeout=5,
                                  check_same_thread=False)
        self.db.execute('PRAGMA busy_timeout=5000')
        self.path.chmod(0o600)
        self.db.execute('CREATE TABLE IF NOT EXISTS owner_release_identity '
                        '(owner TEXT PRIMARY KEY, token_hmac TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS owner_release_results '
                        '(owner TEXT NOT NULL, release TEXT NOT NULL, content TEXT NOT NULL, '
                        'fetched_at REAL NOT NULL, expires_at REAL NOT NULL, '
                        'PRIMARY KEY(owner,release))')
        if created:
            with self.db:
                self.db.execute('DELETE FROM owner_release_results')
                self.db.execute('DELETE FROM owner_release_identity')

    @staticmethod
    def _key(path: Path) -> tuple[bytes, bool]:
        created = False
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                         getattr(os, 'O_NOFOLLOW', 0), 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, 'wb') as out:
                out.write(os.urandom(32))
            created = True
        if path.is_symlink() or stat.S_IMODE(path.stat().st_mode) != 0o600:
            raise OSError('invalid cache key permissions')
        key = path.read_bytes()
        if len(key) != 32:
            raise OSError('invalid cache key')
        return key, created

    def close(self):
        self.db.close()

    def _fingerprint(self, token: str) -> str:
        return hmac.new(self.key, token.encode(), hashlib.sha256).hexdigest()

    def _bind(self, owner: str, fingerprint: str):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('SELECT token_hmac FROM owner_release_identity '
                                  'WHERE owner=?', (owner,)).fetchone()
            if row is not None and row[0] != fingerprint:
                self.db.execute('DELETE FROM owner_release_results WHERE owner=?', (owner,))
            self.db.execute('INSERT INTO owner_release_identity(owner,token_hmac) '
                            'VALUES (?,?) ON CONFLICT(owner) DO UPDATE SET '
                            'token_hmac=excluded.token_hmac', (owner, fingerprint))

    def invalidate(self, owner: str):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.db.execute('DELETE FROM owner_release_results WHERE owner=?', (owner,))
            self.db.execute('DELETE FROM owner_release_identity WHERE owner=?', (owner,))

    def _hit(self, owner: str, release: str, now: float):
        row = self.db.execute('SELECT content,fetched_at,expires_at '
                              'FROM owner_release_results WHERE owner=? AND release=?',
                              (owner, release)).fetchone()
        if row is None or now >= row[2]:
            return None
        return row[0], max(0, int((now - row[1]) * 1000))

    def _put(self, owner: str, release: str, fingerprint: str, content: str):
        now = self.clock()
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            row = self.db.execute('SELECT token_hmac FROM owner_release_identity '
                                  'WHERE owner=?', (owner,)).fetchone()
            if row is None or row[0] != fingerprint:
                return  # credential changed while noam was running
            self.db.execute('INSERT INTO owner_release_results '
                            '(owner,release,content,fetched_at,expires_at) VALUES (?,?,?,?,?) '
                            'ON CONFLICT(owner,release) DO UPDATE SET '
                            'content=excluded.content,fetched_at=excluded.fetched_at,'
                            'expires_at=excluded.expires_at',
                            (owner, release, content, now, now + self.TTL_S))
            while True:
                count, size = self.db.execute(
                    'SELECT COUNT(*),COALESCE(SUM(LENGTH(CAST(content AS BLOB))),0) '
                    'FROM owner_release_results WHERE owner=?', (owner,)).fetchone()
                if count <= self.MAX_OWNER_ENTRIES and size <= self.MAX_OWNER_BYTES:
                    break
                self.db.execute('DELETE FROM owner_release_results WHERE rowid=('
                                'SELECT rowid FROM owner_release_results WHERE owner=? '
                                'ORDER BY fetched_at ASC LIMIT 1)', (owner,))

    def lookup(self, owner: str, release: str, token: str, fetch, *, deadline=None,
               on_miss=None):
        """Return (content, cache_hit, cache_age_ms, lock_wait_ms)."""
        key = release.casefold()
        fingerprint = self._fingerprint(token)
        started = time.monotonic()
        lock_name = hashlib.sha256((owner + '\0' + key).encode()).hexdigest()
        lock_path = self.path.with_name(self.path.name + '.locks') / (lock_name + '.lock')
        while True:
            self._bind(owner, fingerprint)
            hit = self._hit(owner, key, self.clock())
            if hit is not None:
                return hit[0], True, hit[1], int((time.monotonic() - started) * 1000)
            try:
                with FileLock(lock_path):
                    wait_ms = int((time.monotonic() - started) * 1000)
                    self._bind(owner, fingerprint)
                    hit = self._hit(owner, key, self.clock())
                    if hit is not None:
                        return hit[0], True, hit[1], wait_ms
                    if on_miss is not None:
                        on_miss()
                    text = fetch()
                    try:
                        self._put(owner, key, fingerprint, text)
                    except sqlite3.Error:
                        pass  # a cache write failure must not repeat a successful noam call
                    return text, False, None, wait_ms
            except LockBusy:
                if deadline is not None and time.monotonic() >= deadline:
                    raise QueryError('timeout') from None
                time.sleep(0.05)

    def expire(self):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            self.db.execute('DELETE FROM owner_release_results WHERE expires_at<=?',
                            (self.clock(),))
