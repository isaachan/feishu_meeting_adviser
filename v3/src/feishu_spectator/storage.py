"""Atomic page ingestion with idempotent event storage on local SQLite."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sqlite3
import time

from .infrastructure import private_directory
from .models import MeetingItem, digest

SCHEMA_VERSION = '1'


class StorageError(Exception):
    pass


class CapacityError(StorageError):
    pass


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def decode_mapping(body: str) -> dict:
    try:
        value = json.loads(body)
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, TypeError):
        raise StorageError('invalid stored record; body hidden, investigate without deleting data') from None


@dataclass
class PageCommit:
    new_items: int
    duplicates: int
    new_item_list: list
    checkpoint: dict

    @property
    def total(self) -> int:
        return self.new_items + self.duplicates


class Store:
    def __init__(self, path: Path, *, max_database_bytes: int = 150_000_000, readonly: bool = False):
        self.path = Path(path)
        self.max_database_bytes = max_database_bytes
        self.readonly = readonly
        if not self.path.exists():
            raise StorageError('no spectator database; run listen first')
        if self.path.is_symlink() or not self.path.is_file():
            raise StorageError('spectator database must be a regular local file')
        try:
            self.db = sqlite3.connect(self.path, isolation_level=None,
                                      uri=f'file:{self.path}?mode=ro' if readonly else None)
            self.db.row_factory = sqlite3.Row
            if not readonly:
                self.db.execute('PRAGMA journal_mode=WAL')
                self.db.execute('PRAGMA synchronous=FULL')
            self._require_schema()
        except sqlite3.Error:
            raise StorageError('local database operation failed; check space and permissions') from None

    @classmethod
    def initialize(cls, path: Path, *, max_database_bytes: int = 150_000_000) -> None:
        path = Path(path)
        private_directory(path.parent)
        if path.exists():
            return
        db = None
        try:
            db = sqlite3.connect(path, isolation_level=None)
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=FULL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS schema_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS meetings(
                    meeting_id TEXT PRIMARY KEY,
                    checkpoint TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events(
                    meeting_id TEXT NOT NULL,
                    item_key TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    body TEXT NOT NULL,
                    PRIMARY KEY(meeting_id, item_key)
                );
            ''')
            db.execute('INSERT OR REPLACE INTO schema_meta(key, value) VALUES (?, ?)',
                       ('version', SCHEMA_VERSION))
            db.close()
            db = None
            os.chmod(path, 0o600)
        except sqlite3.Error:
            raise StorageError('local database initialization failed') from None
        finally:
            if db is not None:
                db.close()

    def _require_schema(self):
        row = self.db.execute('SELECT value FROM schema_meta WHERE key=?', ('version',)).fetchone()
        if row is None or row['value'] != SCHEMA_VERSION:
            raise StorageError('incompatible spectator database schema; investigate before deleting')

    def close(self):
        self.db.close()

    def physical_bytes(self) -> int:
        return sum(path.stat().st_size for path in
                   (self.path, Path(str(self.path) + '-wal'), Path(str(self.path) + '-shm'))
                   if path.exists())

    def ensure_meeting(self, meeting_id: str, checkpoint: dict) -> dict:
        if self.readonly:
            raise StorageError('readonly store cannot create meetings')
        try:
            self.db.execute('BEGIN IMMEDIATE')
            self.db.execute('INSERT OR IGNORE INTO meetings(meeting_id, checkpoint) VALUES (?, ?)',
                            (meeting_id, encode(checkpoint)))
            self.db.execute('COMMIT')
        except sqlite3.Error:
            self._rollback()
            raise StorageError('local database operation failed') from None
        return self.checkpoint(meeting_id)

    def checkpoint(self, meeting_id: str) -> dict:
        row = self.db.execute('SELECT checkpoint FROM meetings WHERE meeting_id=?', (meeting_id,)).fetchone()
        if row is None:
            raise StorageError('unknown meeting')
        return decode_mapping(row['checkpoint'])

    def _rollback(self):
        if self.db.in_transaction:
            self.db.execute('ROLLBACK')

    def commit_page(self, meeting_id: str, items: list[MeetingItem], checkpoint: dict) -> PageCommit:
        """Atomically persist new items and the advanced checkpoint.

        Idempotent: (meeting_id, item_key) duplicates are ignored, so a cursor
        reset that re-pulls a page never stores the same item twice.
        """
        if self.readonly:
            raise StorageError('readonly store cannot commit pages')
        new_items = duplicates = 0
        new_item_list = []
        try:
            self.db.execute('BEGIN IMMEDIATE')
            for item in items:
                body = encode(item.to_dict())
                cursor = self.db.execute(
                    'INSERT OR IGNORE INTO events(meeting_id, item_key, kind, body) VALUES (?, ?, ?, ?)',
                    (meeting_id, item.item_key, item.kind.value, body))
                if cursor.rowcount:
                    new_items += 1
                    new_item_list.append(item)
                else:
                    duplicates += 1
            self.db.execute('UPDATE meetings SET checkpoint=? WHERE meeting_id=?',
                            (encode(checkpoint), meeting_id))
            if self.db.execute('SELECT changes()').fetchone()[0] != 1:
                raise StorageError('unknown meeting')
            if self.physical_bytes() > self.max_database_bytes:
                raise CapacityError('database capacity reached; page rolled back')
            self.db.execute('COMMIT')
        except sqlite3.Error:
            self._rollback()
            raise StorageError('local database operation failed') from None
        except CapacityError:
            self._rollback()
            raise
        return PageCommit(new_items, duplicates, new_item_list, checkpoint)

    def statistics(self, meeting_id: str) -> dict:
        rows = self.db.execute(
            'SELECT kind, COUNT(*) AS n FROM events WHERE meeting_id=? GROUP BY kind', (meeting_id,)).fetchall()
        return {'stored_items': {row['kind']: row['n'] for row in rows},
                'meeting_hash': digest(meeting_id)[:12]}

    def recent_items(self, meeting_id: str, *, limit: int = 100) -> list[dict]:
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError('limit must be within 1..1000')
        rows = self.db.execute(
            'SELECT body FROM events WHERE meeting_id=? ORDER BY rowid DESC LIMIT ?', (meeting_id, limit)).fetchall()
        # rowid follows insertion order; items arrive in commit order.
        return [decode_mapping(row['body']) for row in rows]
