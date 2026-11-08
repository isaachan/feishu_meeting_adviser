"""Persistent inference tasks: a row exists before any 202 response.

State machine (docs/CONTRACT_REMOTE_INFERENCE.md):

    queued -> running -> succeeded | failed
    queued -> cancelled            (cancel while waiting)
    running -> cancelled           (worker observes cancel_requested)
    succeeded | failed | cancelled -> expired   (retention sweep, details cleared)

Resubmitting the same request_id with the same content_hash after failed,
cancelled or expired requeues the task; succeeded never re-executes.
"""
from contextlib import contextmanager
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA_VERSION = 1
STATES = ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'expired')
REQUEUEABLE = ('failed', 'cancelled', 'expired')

CURRENT_COLUMNS = {
    'tasks': {
        'request_id', 'content_hash', 'backend', 'model', 'system', 'input',
        'output', 'deadline_seconds', 'metadata', 'state', 'error_code',
        'result', 'timings', 'created_ns', 'started_ns', 'finished_ns',
        'cancel_requested', 'attempts',
    },
}

CURRENT_SCHEMA = (
    '''CREATE TABLE tasks (
        request_id TEXT PRIMARY KEY, content_hash TEXT NOT NULL,
        backend TEXT NOT NULL, model TEXT, system TEXT,
        input TEXT, output TEXT NOT NULL,
        deadline_seconds INTEGER NOT NULL, metadata TEXT,
        state TEXT NOT NULL, error_code TEXT, result TEXT, timings TEXT,
        created_ns INTEGER NOT NULL, started_ns INTEGER, finished_ns INTEGER,
        cancel_requested INTEGER NOT NULL DEFAULT 0,
        attempts INTEGER NOT NULL DEFAULT 0)''',
    'CREATE INDEX task_state ON tasks(state, created_ns)',
)


class StorageError(Exception):
    """Safe local error; never carries payload content."""


def _tables(db: sqlite3.Connection) -> set[str]:
    return {row['name'] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row['name'] for row in db.execute(f'PRAGMA table_info({table})')}


def _require_schema(db: sqlite3.Connection) -> None:
    if (db.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION
            or _tables(db) != set(CURRENT_COLUMNS)
            or any(_columns(db, table) != columns
                   for table, columns in CURRENT_COLUMNS.items())):
        raise StorageError(
            'gateway database schema is unsupported; move the old file aside '
            'and initialize again')


def _open(path: Path, *, create: bool) -> sqlite3.Connection:
    path = Path(path)
    if create:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink() or path.exists() and not path.is_file():
            raise StorageError('gateway database must be a regular file')
        existed = path.exists()
        db = sqlite3.connect(str(path), isolation_level=None, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA busy_timeout=5000')
        if not existed:
            with db:
                for statement in CURRENT_SCHEMA:
                    db.execute(statement)
                db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
        _require_schema(db)
        return db
    if not path.exists() or path.is_symlink() or not path.is_file():
        raise StorageError('gateway database does not exist')
    db = sqlite3.connect(str(path), isolation_level=None, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA busy_timeout=5000')
    db.execute('PRAGMA query_only=ON')
    _require_schema(db)
    return db


class GatewayStore:
    """One SQLite connection guarded by a lock; all writes are short transactions."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.RLock()
        self._db = _open(self.path, create=True)

    @classmethod
    def open_readonly(cls, path: Path) -> 'GatewayStore':
        instance = cls.__new__(cls)
        instance.path = Path(path)
        instance._lock = threading.RLock()
        instance._db = _open(instance.path, create=False)
        return instance

    @contextmanager
    def _writing(self):
        with self._lock:
            try:
                with self._db:
                    yield
            except sqlite3.Error:
                raise StorageError('gateway database write failed') from None

    def _require(self, request_id: str) -> dict:
        task = self.get(request_id)
        if task is None:
            raise StorageError('task disappeared during update')
        return task

    def close(self):
        with self._lock:
            self._db.close()

    def _row(self, request_id: str) -> sqlite3.Row | None:
        return self._db.execute(
            'SELECT * FROM tasks WHERE request_id=?', (request_id,)).fetchone()

    def get(self, request_id: str) -> dict | None:
        with self._lock:
            row = self._row(request_id)
            return dict(row) if row else None

    def insert(self, *, request_id: str, content_hash: str, backend: str,
               model: str | None, system: str, input_json: str, output: str,
               deadline_seconds: int, metadata_json: str) -> dict:
        now_ns = time.time_ns()
        with self._writing():
            self._db.execute(
                'INSERT INTO tasks(request_id,content_hash,backend,model,system,input,output,'
                'deadline_seconds,metadata,state,created_ns) VALUES (?,?,?,?,?,?,?,?,?,?,?)',
                (request_id, content_hash, backend, model, system, input_json, output,
                 deadline_seconds, metadata_json, 'queued', now_ns))
        return self._require(request_id)

    def requeue(self, request_id: str) -> dict:
        with self._writing():
            changed = self._db.execute(
                'UPDATE tasks SET state=\'queued\',error_code=NULL,result=NULL,timings=NULL,'
                'cancel_requested=0,attempts=attempts+1,started_ns=NULL,finished_ns=NULL '
                'WHERE request_id=? AND state IN (?,?,?)',
                (request_id, *REQUEUEABLE)).rowcount
            if changed != 1:
                raise StorageError('task is not requeueable')
        return self._require(request_id)

    def claim(self, request_id: str) -> dict:
        with self._writing():
            changed = self._db.execute(
                'UPDATE tasks SET state=\'running\',started_ns=? WHERE request_id=? '
                'AND state=\'queued\' AND cancel_requested=0',
                (time.time_ns(), request_id)).rowcount
            if changed != 1:
                raise StorageError('task is no longer queued')
        return self._require(request_id)

    def finish(self, request_id: str, state: str, *, result_json: str | None = None,
               error_code: str | None = None, timings_json: str | None = None) -> dict:
        if state not in ('succeeded', 'failed', 'cancelled'):
            raise StorageError('invalid finish state')
        with self._writing():
            cancelled = self._db.execute(
                'SELECT cancel_requested FROM tasks WHERE request_id=? AND state=\'running\'',
                (request_id,)).fetchone()
            if cancelled is None:
                raise StorageError('task is not running')
            if cancelled['cancel_requested'] and state == 'succeeded':
                # A cancelled client never receives the result; the execution
                # outcome is recorded as cancelled either way.
                state = 'cancelled'
            self._db.execute(
                'UPDATE tasks SET state=?,result=?,error_code=?,timings=?,finished_ns=? '
                'WHERE request_id=? AND state=\'running\'',
                (state, result_json, error_code, timings_json, time.time_ns(), request_id))
        return self._require(request_id)

    def request_cancel(self, request_id: str) -> dict:
        with self._writing():
            row = self._row(request_id)
            if row is None:
                raise StorageError('unknown task')
            if row['state'] == 'queued':
                self._db.execute(
                    'UPDATE tasks SET state=\'cancelled\',error_code=\'cancelled\','
                    'finished_ns=? WHERE request_id=? AND state=\'queued\'',
                    (time.time_ns(), request_id))
            elif row['state'] == 'running':
                self._db.execute(
                    'UPDATE tasks SET cancel_requested=1 WHERE request_id=? '
                    'AND state=\'running\'', (request_id,))
        return self._require(request_id)

    def cancel_requested(self, request_id: str) -> bool:
        with self._lock:
            row = self._row(request_id)
            return bool(row and row['cancel_requested'])

    def queued_count(self) -> int:
        with self._lock:
            return self._db.execute(
                'SELECT count(*) FROM tasks WHERE state=\'queued\'').fetchone()[0]

    def next_queued(self) -> dict | None:
        """Pop the oldest queued task by marking it running (for worker pickup)."""
        with self._lock, self._db:
            row = self._db.execute(
                'SELECT request_id FROM tasks WHERE state=\'queued\' AND cancel_requested=0 '
                'ORDER BY created_ns LIMIT 1').fetchone()
            if row is None:
                return None
            return self.claim(row['request_id'])

    def recover_interrupted(self) -> int:
        """Startup recovery: anything left mid-flight failed with a retryable code."""
        with self._writing():
            return self._db.execute(
                'UPDATE tasks SET state=\'failed\',error_code=\'backend_unavailable\','
                'finished_ns=? WHERE state IN (\'queued\',\'running\')',
                (time.time_ns(),)).rowcount

    def expire(self, *, retention_hours: float, now: float | None = None) -> int:
        moment = time.time() if now is None else now
        cutoff_ns = int((moment - retention_hours * 3600) * 1_000_000_000)
        with self._writing():
            return self._db.execute(
                'UPDATE tasks SET state=\'expired\',result=NULL,timings=NULL,system=NULL,'
                'input=NULL,metadata=NULL,error_code=NULL WHERE state IN '
                '(\'succeeded\',\'failed\',\'cancelled\') AND finished_ns IS NOT NULL '
                'AND finished_ns<?',
                (cutoff_ns,)).rowcount
