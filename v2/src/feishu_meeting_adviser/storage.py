"""Atomic page ingestion and independent durable consumer progress on local SQLite."""
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path
import json
import os
import sqlite3
import stat
import time
from typing import Callable

from .models import Caption, ChangeKind, Delivery, IngestMode, SourceState, Status, digest


class StorageError(Exception):
    """Safe local error, never includes database contents or a sensitive path."""


class CapacityError(StorageError):
    pass


def private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise StorageError('data directory must be a real directory with mode 700')


def connect_private(path: Path) -> sqlite3.Connection:
    private_directory(path.parent)
    if path.is_symlink():
        raise StorageError('database must not be a symlink')
    if path.exists() and (not path.is_file() or path.stat().st_mode & 0o077):
        raise StorageError('database must be a regular file with mode 600')
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    os.close(fd)
    db = sqlite3.connect(path, isolation_level=None, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA synchronous=FULL')
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA busy_timeout=5000')
    return db


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def caption_from_json(body: str) -> Caption:
    value = json.loads(body)
    value['mode'] = IngestMode(value['mode'])
    return Caption(**value)


class Store:
    def __init__(self, path: Path, *, max_payload_bytes: int = 100_000_000):
        self.path = Path(path)
        self.max_payload_bytes = max_payload_bytes
        self.opened_ns = time.time_ns()
        self.db = connect_private(self.path)
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 1, 2):
            self.close()
            raise StorageError('unsupported database schema; do not discard existing data')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS meetings (
                meeting_id TEXT PRIMARY KEY, checkpoint TEXT NOT NULL, state TEXT
            );
            CREATE TABLE IF NOT EXISTS captions (
                meeting_id TEXT NOT NULL REFERENCES meetings(meeting_id),
                sentence_key TEXT NOT NULL, fingerprint TEXT NOT NULL, body TEXT NOT NULL,
                PRIMARY KEY (meeting_id, sentence_key)
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                meeting_id TEXT NOT NULL REFERENCES meetings(meeting_id),
                delivery_id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, body TEXT NOT NULL,
                created_ns INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS deliveries_meeting ON deliveries(meeting_id, sequence);
            CREATE TABLE IF NOT EXISTS consumers (
                meeting_id TEXT NOT NULL REFERENCES meetings(meeting_id), consumer_id TEXT NOT NULL,
                acknowledged INTEGER NOT NULL DEFAULT 0, offered INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (meeting_id, consumer_id)
            );
            CREATE TABLE IF NOT EXISTS attempts (
                meeting_id TEXT NOT NULL, consumer_id TEXT NOT NULL,
                sequence INTEGER NOT NULL REFERENCES deliveries(sequence) ON DELETE CASCADE,
                count INTEGER NOT NULL, PRIMARY KEY (meeting_id, consumer_id, sequence)
            );
            CREATE TABLE IF NOT EXISTS risks (
                meeting_id TEXT NOT NULL REFERENCES meetings(meeting_id), identity TEXT NOT NULL,
                body TEXT NOT NULL, PRIMARY KEY (meeting_id,identity)
            );
            PRAGMA user_version=2;
        ''')

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def transaction(self):
        try:
            self.db.execute('BEGIN IMMEDIATE')
            yield
            self._check_capacity()
            self.db.execute('COMMIT')
        except sqlite3.Error as exc:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise StorageError('local database operation failed; check space and permissions') from None
        except BaseException:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise

    def payload_bytes(self) -> int:
        total = 0
        for table, columns in (('meetings', ('checkpoint', 'state')), ('captions', ('body',)), ('deliveries', ('body',)), ('risks', ('body',))):
            for column in columns:
                total += self.db.execute(f'SELECT COALESCE(SUM(length(CAST({column} AS BLOB))),0) FROM {table}').fetchone()[0]
        return total

    def _check_capacity(self) -> None:
        if self.payload_bytes() > self.max_payload_bytes:
            raise CapacityError('content capacity reached; checkpoint not advanced')

    def ensure_meeting(self, meeting_id: str, checkpoint: dict) -> dict:
        with self.transaction():
            self.db.execute('INSERT OR IGNORE INTO meetings(meeting_id,checkpoint) VALUES (?,?)',
                            (meeting_id, encode(checkpoint)))
        return self.checkpoint(meeting_id)

    def checkpoint(self, meeting_id: str) -> dict:
        row = self.db.execute('SELECT checkpoint FROM meetings WHERE meeting_id=?', (meeting_id,)).fetchone()
        if row is None:
            raise StorageError('unknown meeting')
        return json.loads(row['checkpoint'])

    def caption(self, meeting_id: str, key: str) -> Caption | None:
        row = self.db.execute('SELECT body FROM captions WHERE meeting_id=? AND sentence_key=?', (meeting_id, key)).fetchone()
        return caption_from_json(row['body']) if row else None

    def _deliver(self, meeting_id: str, kind: ChangeKind, body: dict, identity) -> bool:
        result = self.db.execute('INSERT OR IGNORE INTO deliveries(meeting_id,delivery_id,kind,body,created_ns) VALUES (?,?,?,?,?)',
                                (meeting_id, digest(identity), kind.value, encode(body), time.time_ns()))
        return result.rowcount == 1

    def _status(self, status: Status) -> None:
        if status.state == SourceState.INCOMPLETE:
            identity = digest([status.reason, status.gap_start, status.gap_end])
            self.db.execute('INSERT OR IGNORE INTO risks VALUES (?,?,?)',
                            (status.meeting_id, identity, encode(asdict(status))))
        previous = self.current_state(status.meeting_id)
        if previous and (previous.state, previous.reason, previous.gap_start, previous.gap_end) == (
                status.state, status.reason, status.gap_start, status.gap_end):
            return
        self.db.execute('UPDATE meetings SET state=? WHERE meeting_id=?', (encode(asdict(status)), status.meeting_id))
        self._deliver(status.meeting_id, ChangeKind.STATUS, asdict(status), ['status', asdict(status), time.time_ns()])

    def publish_status(self, status: Status) -> None:
        with self.transaction():
            if not self.db.execute('SELECT 1 FROM meetings WHERE meeting_id=?', (status.meeting_id,)).fetchone():
                raise StorageError('unknown meeting')
            self._status(status)

    def current_state(self, meeting_id: str) -> Status | None:
        row = self.db.execute('SELECT state FROM meetings WHERE meeting_id=?', (meeting_id,)).fetchone()
        if not row or not row['state']:
            return None
        value = json.loads(row['state'])
        value['state'] = SourceState(value['state'])
        return Status(**value)

    def active_status(self, meeting_id: str, at: str) -> Status:
        row = self.db.execute('SELECT body FROM risks WHERE meeting_id=? ORDER BY rowid DESC LIMIT 1', (meeting_id,)).fetchone()
        if row:
            value = json.loads(row['body'])
            value['state'] = SourceState.INCOMPLETE
            value['at'] = at
            return Status(**value)
        return Status(meeting_id, SourceState.COLLECTING, 'scan_success', at)

    def commit_page(self, meeting_id: str, captions: list[Caption], checkpoint: dict, *,
                    fault: Callable[[str], None] | None = None, statuses: tuple[Status, ...] = ()) -> dict[str, int]:
        counts = {'added': 0, 'updated': 0, 'conflict': 0, 'duplicates': 0, 'older': 0}
        with self.transaction():
            if not self.db.execute('SELECT 1 FROM meetings WHERE meeting_id=?', (meeting_id,)).fetchone():
                raise StorageError('unknown meeting')
            for candidate in captions:
                if candidate.meeting_id != meeting_id:
                    raise StorageError('caption meeting mismatch; page not committed')
                old = self.caption(meeting_id, candidate.sentence_key)
                if old and old.fingerprint == candidate.fingerprint:
                    counts['duplicates'] += 1
                    if candidate.source_revision is not None and (old.source_revision is None or candidate.source_revision > old.source_revision):
                        self.db.execute('UPDATE captions SET body=? WHERE meeting_id=? AND sentence_key=?',
                                        (encode(asdict(candidate)), meeting_id, candidate.sentence_key))
                    continue
                kind = ChangeKind.ADDED
                if old:
                    if old.source_revision is not None and candidate.source_revision is not None:
                        if candidate.source_revision < old.source_revision:
                            counts['older'] += 1
                            continue
                        kind = ChangeKind.UPDATED if candidate.source_revision > old.source_revision else ChangeKind.CONFLICT
                    else:
                        kind = ChangeKind.CONFLICT
                identity = ['caption', meeting_id, candidate.sentence_key, candidate.fingerprint, candidate.source_revision, kind.value]
                if not self._deliver(meeting_id, kind, asdict(candidate), identity):
                    counts['duplicates'] += 1
                    continue
                counts[kind.value if kind != ChangeKind.CONFLICT else 'conflict'] += 1
                if kind != ChangeKind.CONFLICT:
                    self.db.execute('INSERT INTO captions(meeting_id,sentence_key,fingerprint,body) VALUES (?,?,?,?) '
                                    'ON CONFLICT(meeting_id,sentence_key) DO UPDATE SET fingerprint=excluded.fingerprint,body=excluded.body',
                                    (meeting_id, candidate.sentence_key, candidate.fingerprint, encode(asdict(candidate))))
                else:
                    self._status(Status(meeting_id, SourceState.INCOMPLETE, 'revision_conflict', candidate.received_at))
            if fault:
                fault('after_captions')
            self.db.execute('UPDATE meetings SET checkpoint=? WHERE meeting_id=?', (encode(checkpoint), meeting_id))
            for status in statuses:
                if status.meeting_id != meeting_id:
                    raise StorageError('status meeting mismatch; page not committed')
                self._status(status)
            if fault:
                fault('after_checkpoint')
        if fault:
            fault('after_commit')
        return counts

    def read(self, meeting_id: str, consumer_id: str, *, limit: int = 100) -> list[Delivery]:
        if not consumer_id or len(consumer_id) > 128 or not 1 <= limit <= 1000:
            raise StorageError('invalid consumer or read limit')
        result = []
        with self.transaction():
            self.db.execute('INSERT OR IGNORE INTO consumers(meeting_id,consumer_id) VALUES (?,?)', (meeting_id, consumer_id))
            ack = self.db.execute('SELECT acknowledged FROM consumers WHERE meeting_id=? AND consumer_id=?', (meeting_id, consumer_id)).fetchone()[0]
            rows = self.db.execute('SELECT d.*,COALESCE(a.count,0) AS attempt_count FROM deliveries d LEFT JOIN attempts a '
                                   'ON a.sequence=d.sequence AND a.meeting_id=d.meeting_id AND a.consumer_id=? '
                                   'WHERE d.meeting_id=? AND d.sequence>? ORDER BY d.sequence LIMIT ?',
                                   (consumer_id, meeting_id, ack, limit)).fetchall()
            for row in rows:
                body = json.loads(row['body'])
                value = {'sequence': row['sequence'], 'delivery_id': row['delivery_id'], 'kind': row['kind'],
                         'caption': body if row['kind'] != ChangeKind.STATUS else None,
                         'status': body if row['kind'] == ChangeKind.STATUS else None,
                         'replayed': row['attempt_count'] > 0 or row['created_ns'] < self.opened_ns}
                result.append(Delivery.from_dict(value))
                self.db.execute('INSERT INTO attempts VALUES (?,?,?,1) ON CONFLICT(meeting_id,consumer_id,sequence) '
                                'DO UPDATE SET count=count+1', (meeting_id, consumer_id, row['sequence']))
            if result:
                self.db.execute('UPDATE consumers SET offered=MAX(offered,?) WHERE meeting_id=? AND consumer_id=?',
                                (result[-1].sequence, meeting_id, consumer_id))
        return result

    def acknowledge(self, meeting_id: str, consumer_id: str, sequence: int) -> None:
        if type(sequence) is not int:
            raise StorageError('invalid acknowledgement')
        with self.transaction():
            row = self.db.execute('SELECT * FROM consumers WHERE meeting_id=? AND consumer_id=?', (meeting_id, consumer_id)).fetchone()
            if not row or sequence < row['acknowledged'] or sequence > row['offered']:
                raise StorageError('acknowledgement outside delivered contiguous prefix')
            if sequence == row['acknowledged']:
                return
            if not self.db.execute('SELECT 1 FROM deliveries WHERE meeting_id=? AND sequence=?', (meeting_id, sequence)).fetchone():
                raise StorageError('acknowledgement meeting mismatch')
            self.db.execute('UPDATE consumers SET acknowledged=? WHERE meeting_id=? AND consumer_id=?', (sequence, meeting_id, consumer_id))

    def backlog(self, meeting_id: str, consumer_id: str) -> int:
        row = self.db.execute('SELECT acknowledged FROM consumers WHERE meeting_id=? AND consumer_id=?', (meeting_id, consumer_id)).fetchone()
        return self.db.execute('SELECT count(*) FROM deliveries WHERE meeting_id=? AND sequence>?',
                               (meeting_id, row[0] if row else 0)).fetchone()[0]


class DurableFakeConsumer:
    """Persist context and idempotency before separately acknowledging the source."""
    def __init__(self, path: Path):
        self.db = connect_private(Path(path))
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS accepted (delivery_id TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS context (meeting_id TEXT, sentence_key TEXT, body TEXT NOT NULL,
                                               PRIMARY KEY (meeting_id,sentence_key));
        ''')

    def accept(self, delivery: Delivery) -> bool:
        try:
            self.db.execute('BEGIN IMMEDIATE')
            inserted = self.db.execute('INSERT OR IGNORE INTO accepted VALUES (?,?)',
                                       (delivery.delivery_id, encode(delivery.to_dict()))).rowcount
            if inserted and delivery.caption and delivery.kind in (ChangeKind.ADDED, ChangeKind.UPDATED):
                caption = delivery.caption
                self.db.execute('INSERT INTO context VALUES (?,?,?) ON CONFLICT(meeting_id,sentence_key) DO UPDATE SET body=excluded.body',
                                (caption.meeting_id, caption.sentence_key, encode(asdict(caption))))
            self.db.execute('COMMIT')
            return bool(inserted)
        except sqlite3.Error:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise StorageError('fake consumer persistence failed; input not acknowledged') from None

    def context(self, meeting_id: str) -> list[Caption]:
        return [caption_from_json(row['body']) for row in self.db.execute('SELECT body FROM context WHERE meeting_id=? ORDER BY sentence_key', (meeting_id,))]

    def close(self) -> None:
        self.db.close()
