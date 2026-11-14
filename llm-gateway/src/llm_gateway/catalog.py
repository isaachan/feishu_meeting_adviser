"""Asynchronous, meeting-scoped NSDP catalog snapshots."""
from concurrent.futures import ThreadPoolExecutor
import json
import logging
from pathlib import Path
import sqlite3
import threading
import time

from ._private import private_directory
from .nsdp_query import QueryError

LOG = logging.getLogger(__name__)


class MeetingCatalogs:
    """The first successful lookup is frozen for the lifetime of a meeting."""

    def __init__(self, path: Path, query, *, workers=2):
        self.path = Path(path)
        private_directory(self.path.parent)
        if self.path.is_symlink():
            raise ValueError('catalog database must not be a symlink')
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        self.path.chmod(0o600)
        self.db.execute('CREATE TABLE IF NOT EXISTS catalogs '
                        '(session_id TEXT PRIMARY KEY, owner TEXT NOT NULL, '
                        'content TEXT, fetched_at REAL, updated_at REAL NOT NULL)')
        self.db.commit()
        self.query = query
        self.lock = threading.RLock()
        self.pending: set[str] = set()
        self.failed_until: dict[str, float] = {}
        self.pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix='catalog')

    def prepare(self, session_id: str, owner: str) -> None:
        with self.lock:
            row = self.db.execute('SELECT owner,content FROM catalogs WHERE session_id=?',
                                  (session_id,)).fetchone()
            if row and row[0] != owner:
                raise ValueError('session owner conflict')
            if row and row[1] is not None:
                return
            if session_id in self.pending or time.monotonic() < self.failed_until.get(session_id, 0):
                return
            if not row:
                self.db.execute('INSERT INTO catalogs(session_id,owner,updated_at) VALUES (?,?,?)',
                                (session_id, owner, time.time()))
                self.db.commit()
            self.pending.add(session_id)
            self.pool.submit(self._fetch, session_id, owner)

    def _fetch(self, session_id: str, owner: str) -> None:
        try:
            catalog = self.query.info(owner)
            content = json.dumps(catalog, ensure_ascii=False, separators=(',', ':'))
            now = time.time()
            with self.lock:
                self.db.execute('UPDATE catalogs SET content=?,fetched_at=?,updated_at=? '
                                'WHERE session_id=? AND owner=? AND content IS NULL',
                                (content, now, now, session_id, owner))
                self.db.commit()
            LOG.info('meeting catalog ready: session=%s', session_id)
        except Exception as exc:  # noqa: BLE001 - background failures never block inference
            code = exc.code if isinstance(exc, QueryError) else 'unavailable'
            LOG.warning('meeting catalog unavailable: session=%s code=%s', session_id, code)
            with self.lock:
                self.failed_until[session_id] = time.monotonic() + 30
        finally:
            with self.lock:
                self.pending.discard(session_id)

    def get(self, session_id: str, owner: str) -> dict | None:
        with self.lock:
            row = self.db.execute('SELECT content,fetched_at FROM catalogs '
                                  'WHERE session_id=? AND owner=?', (session_id, owner)).fetchone()
            if row:
                self.db.execute('UPDATE catalogs SET updated_at=? WHERE session_id=? AND owner=?',
                                (time.time(), session_id, owner))
                self.db.commit()
        if not row or row[0] is None:
            return None
        return {'source': 'nsdp', 'fetched_at': row[1], 'data': json.loads(row[0])}

    def expire(self, *, retention_hours=24):
        with self.lock:
            cutoff = time.time() - retention_hours * 3600
            # Never expire a lookup that is still running.
            rows = self.db.execute('SELECT session_id FROM catalogs WHERE updated_at<?',
                                   (cutoff,)).fetchall()
            for (session_id,) in rows:
                if session_id not in self.pending:
                    self.db.execute('DELETE FROM catalogs WHERE session_id=?', (session_id,))
                    self.failed_until.pop(session_id, None)
            self.db.commit()

    def close(self):
        self.pool.shutdown(wait=True, cancel_futures=True)
        with self.lock:
            self.db.close()
