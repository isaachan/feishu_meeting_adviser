"""Atomic page ingestion and independent durable consumer progress on local SQLite."""
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
import json
import os
import sqlite3
import stat
import time
from typing import Callable

from .models import Caption, ChangeKind, Delivery, IngestMode, SourceState, Status, digest, epoch_ns


class StorageError(Exception):
    """Safe local error, never includes database contents or a sensitive path."""


class CapacityError(StorageError):
    pass


def private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise StorageError('data directory must be a real directory with mode 700')


def connect_private(path: Path, *, max_database_bytes: int = 150_000_000) -> sqlite3.Connection:
    private_directory(path.parent)
    if path.is_symlink():
        raise StorageError('database must not be a symlink')
    if path.exists() and (not path.is_file() or path.stat().st_mode & 0o077):
        raise StorageError('database must be a regular file with mode 600')
    fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    os.close(fd)
    db = sqlite3.connect(path, isolation_level=None, timeout=5)
    try:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('PRAGMA synchronous=FULL')
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA secure_delete=ON')
        db.execute('PRAGMA wal_autocheckpoint=256')
        db.execute('PRAGMA journal_size_limit=1048576')
        page_size = db.execute('PRAGMA page_size').fetchone()[0]
        max_pages = max_database_bytes // page_size
        if max_pages < db.execute('PRAGMA page_count').fetchone()[0]:
            raise CapacityError('database already exceeds configured capacity; data preserved')
        db.execute(f'PRAGMA max_page_count={max_pages}')
        db.execute('PRAGMA busy_timeout=5000')
        return db
    except BaseException:
        db.close()
        raise


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def caption_from_json(body: str) -> Caption:
    value = json.loads(body)
    value['mode'] = IngestMode(value['mode'])
    return Caption(**value)


class Store:
    def __init__(self, path: Path, *, max_payload_bytes: int = 100_000_000, max_database_bytes: int = 150_000_000):
        self.path = Path(path)
        self.max_payload_bytes = max_payload_bytes
        self.opened_ns = time.time_ns()
        self.max_database_bytes = max_database_bytes
        self.last_commit_captions = []
        self.db = connect_private(self.path, max_database_bytes=max_database_bytes)
        try:
            self._initialize()
        except BaseException:
            self.close()
            raise

    def _initialize(self):
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 1, 2, 3, 4, 5):
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
            CREATE TABLE IF NOT EXISTS sessions (app_key TEXT PRIMARY KEY, body TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS conflicts (
                meeting_id TEXT NOT NULL, sentence_key TEXT NOT NULL,
                delivery_id TEXT PRIMARY KEY REFERENCES deliveries(delivery_id), resolved INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS memberships (meeting_id TEXT PRIMARY KEY, active INTEGER NOT NULL, left_at TEXT);
            CREATE TABLE IF NOT EXISTS tombstones (identity TEXT PRIMARY KEY, deleted_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS accounting (singleton INTEGER PRIMARY KEY CHECK(singleton=1), bytes INTEGER NOT NULL);
            INSERT OR IGNORE INTO accounting VALUES (1,0);
        ''')
        if version < 5:
            with self.transaction():
                if version in (1, 2):
                    for row in self.db.execute("SELECT meeting_id,delivery_id,body FROM deliveries WHERE kind='conflict'").fetchall():
                        candidate = json.loads(row['body'])
                        self.db.execute('INSERT OR IGNORE INTO conflicts(meeting_id,sentence_key,delivery_id) VALUES (?,?,?)',
                                        (row['meeting_id'], candidate['sentence_key'], row['delivery_id']))
                for row in self.db.execute('SELECT body FROM sessions').fetchall():
                    value = json.loads(row['body'])
                    self._record_membership(value)
                tracked = {'meetings': ('checkpoint','state'), 'captions': ('body','sentence_key','fingerprint'),
                           'deliveries': ('body','delivery_id'), 'risks': ('body',), 'sessions': ('body',),
                           'consumers': ('consumer_id',), 'tombstones': ('identity','deleted_at'),
                           'memberships': ('meeting_id','left_at')}
                total = 0
                for table, columns in tracked.items():
                    expressions = lambda prefix: ' + '.join(f'COALESCE(length(CAST({prefix}{c} AS BLOB)),0)' for c in columns)
                    old, new = expressions('OLD.'), expressions('NEW.')
                    for action, expression in (('insert', new), ('delete', '-(' + old + ')'), ('update', '(' + new + ') - (' + old + ')')):
                        self.db.execute(f'CREATE TRIGGER IF NOT EXISTS account_{table}_{action} AFTER {action.upper()} ON {table} '
                                        f'BEGIN UPDATE accounting SET bytes=bytes+({expression}) WHERE singleton=1; END')
                    total += self.db.execute(f'SELECT COALESCE(SUM({expressions("")}),0) FROM {table}').fetchone()[0]
                self.db.execute('UPDATE accounting SET bytes=? WHERE singleton=1', (total,))
                self.db.execute('PRAGMA user_version=5')

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def transaction(self, *, check_capacity=True):
        try:
            self.db.execute('BEGIN IMMEDIATE')
            yield
            if check_capacity:
                self._check_capacity()
            self.db.execute('COMMIT')
        except sqlite3.Error as exc:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            if getattr(exc, 'sqlite_errorcode', 0) & 255 == sqlite3.SQLITE_FULL:
                raise CapacityError('database capacity or disk space exhausted; transaction rolled back') from None
            raise StorageError('local database operation failed; check space and permissions') from None
        except BaseException:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise

    def payload_bytes(self) -> int:
        return self.db.execute('SELECT bytes FROM accounting WHERE singleton=1').fetchone()[0]

    def physical_bytes(self) -> int:
        return sum(path.stat().st_size for path in (self.path, Path(str(self.path)+'-wal'), Path(str(self.path)+'-shm')) if path.exists())

    def _check_capacity(self) -> None:
        if self.payload_bytes() > self.max_payload_bytes:
            raise CapacityError('content capacity reached; checkpoint not advanced')

    def ensure_meeting(self, meeting_id: str, checkpoint: dict) -> dict:
        with self.transaction():
            if self.db.execute('SELECT 1 FROM tombstones WHERE identity=?', (digest(meeting_id),)).fetchone():
                raise StorageError('meeting data explicitly deleted or expired; automatic recollection refused')
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
        emitted = []
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
                counts[kind.value] += 1
                emitted.append(candidate)
                if kind != ChangeKind.CONFLICT:
                    self.db.execute('INSERT INTO captions(meeting_id,sentence_key,fingerprint,body) VALUES (?,?,?,?) '
                                    'ON CONFLICT(meeting_id,sentence_key) DO UPDATE SET fingerprint=excluded.fingerprint,body=excluded.body',
                                    (meeting_id, candidate.sentence_key, candidate.fingerprint, encode(asdict(candidate))))
                else:
                    self.db.execute('INSERT INTO conflicts(meeting_id,sentence_key,delivery_id) VALUES (?,?,?)',
                                    (meeting_id, candidate.sentence_key, digest(identity)))
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
        self.last_commit_captions = emitted
        return counts

    def session(self, app_key: str) -> dict | None:
        row = self.db.execute('SELECT body FROM sessions WHERE app_key=?', (app_key,)).fetchone()
        return json.loads(row['body']) if row else None

    def _record_membership(self, session: dict) -> None:
        mid = session.get('meeting_id')
        phase = session.get('phase')
        if not mid or phase not in ('joined', 'left', 'leave_uncertain'):
            return
        self.db.execute('INSERT INTO memberships VALUES (?,?,?) ON CONFLICT(meeting_id) '
                        'DO UPDATE SET active=excluded.active,left_at=excluded.left_at',
                        (mid, int(phase != 'left'), session.get('left_at') or session.get('manual_confirmation_at')))

    def save_session(self, app_key: str, session: dict) -> None:
        with self.transaction():
            self._record_membership(session)
            self.db.execute('INSERT INTO sessions VALUES (?,?) ON CONFLICT(app_key) DO UPDATE SET body=excluded.body',
                            (app_key, encode(session)))

    def resolve_conflict(self, meeting_id: str, delivery_id: str, *, at: str) -> bool:
        with self.transaction():
            row = self.db.execute('SELECT c.*,d.body FROM conflicts c JOIN deliveries d USING(delivery_id) '
                                  'WHERE c.delivery_id=? AND c.meeting_id=?', (delivery_id, meeting_id)).fetchone()
            if not row:
                raise StorageError('unknown conflict for saved meeting')
            if row['resolved']:
                return False
            candidate = caption_from_json(row['body'])
            self.db.execute('UPDATE captions SET fingerprint=?,body=? WHERE meeting_id=? AND sentence_key=?',
                            (candidate.fingerprint, encode(asdict(candidate)), meeting_id, candidate.sentence_key))
            self.db.execute('UPDATE conflicts SET resolved=1 WHERE meeting_id=? AND sentence_key=?',
                            (meeting_id, candidate.sentence_key))
            self._deliver(meeting_id, ChangeKind.UPDATED, asdict(candidate), ['manual_resolution', meeting_id, delivery_id])
            if not self.db.execute('SELECT 1 FROM conflicts WHERE meeting_id=? AND resolved=0', (meeting_id,)).fetchone():
                risks = self.db.execute('SELECT identity,body FROM risks WHERE meeting_id=?', (meeting_id,)).fetchall()
                for risk in risks:
                    if json.loads(risk['body'])['reason'] == 'revision_conflict':
                        self.db.execute('DELETE FROM risks WHERE meeting_id=? AND identity=?', (meeting_id, risk['identity']))
            self._status(self.active_status(meeting_id, at))
        return True

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


    def statistics(self, meeting_id: str, consumer_id: str = 'fake-qa') -> dict:
        state = self.current_state(meeting_id)
        return {'state': state.state.value if state else 'not_started', 'reason': state.reason if state else 'not_started',
                'pending': self.backlog(meeting_id, consumer_id), 'content_bytes': self.payload_bytes(),
                'database_bytes': self.physical_bytes(),
                'risk_count': self.db.execute('SELECT count(*) FROM risks WHERE meeting_id=?', (meeting_id,)).fetchone()[0],
                'unresolved_conflicts': self.db.execute('SELECT count(*) FROM conflicts WHERE meeting_id=? AND resolved=0', (meeting_id,)).fetchone()[0]}

    def _forget_meeting(self, meeting_id: str, *, at: str) -> None:
        self.db.execute('DELETE FROM conflicts WHERE meeting_id=?', (meeting_id,))
        self.db.execute('DELETE FROM attempts WHERE meeting_id=?', (meeting_id,))
        self.db.execute('DELETE FROM consumers WHERE meeting_id=?', (meeting_id,))
        self.db.execute('DELETE FROM deliveries WHERE meeting_id=?', (meeting_id,))
        self.db.execute('DELETE FROM captions WHERE meeting_id=?', (meeting_id,))
        self.db.execute('DELETE FROM risks WHERE meeting_id=?', (meeting_id,))
        self.db.execute('DELETE FROM meetings WHERE meeting_id=?', (meeting_id,))
        self.db.execute('DELETE FROM memberships WHERE meeting_id=?', (meeting_id,))
        self.db.execute('INSERT OR IGNORE INTO tombstones VALUES (?,?)', (digest(meeting_id), at))

    def forget_meeting(self, meeting_id: str, *, at: str) -> None:
        # Destructive explicit operation: bypass content quota so full storage can be cleaned.
        with self.transaction(check_capacity=False):
            self._forget_meeting(meeting_id, at=at)
        self.compact()

    def prune(self, cutoff_ns: int, *, at: str) -> dict:
        removed = expired_meetings = 0
        with self.transaction(check_capacity=False):
            inactive = {}
            for row in self.db.execute('SELECT meeting_id,left_at FROM memberships WHERE active=0').fetchall():
                left_at = epoch_ns(row['left_at'])
                if left_at is not None and left_at < cutoff_ns:
                    inactive[row['meeting_id']] = True
            for meeting in self.db.execute('SELECT meeting_id FROM meetings').fetchall():
                mid = meeting['meeting_id']
                floor = self.db.execute('SELECT MIN(acknowledged) FROM consumers WHERE meeting_id=?', (mid,)).fetchone()[0]
                if floor is None:  # With no registered consumer, every delivery is unacknowledged.
                    continue
                max_seq = self.db.execute('SELECT COALESCE(MAX(sequence),0) FROM deliveries WHERE meeting_id=?', (mid,)).fetchone()[0]
                conflicts = self.db.execute('SELECT 1 FROM conflicts WHERE meeting_id=? AND resolved=0', (mid,)).fetchone()
                if mid in inactive and floor >= max_seq and not conflicts:
                    self._forget_meeting(mid, at=at)
                    expired_meetings += 1
                    continue
                eligible = [r['sequence'] for r in self.db.execute(
                    'SELECT d.sequence FROM deliveries d LEFT JOIN conflicts c ON c.delivery_id=d.delivery_id '
                    'WHERE d.meeting_id=? AND d.sequence<=? AND d.created_ns<? AND (c.resolved IS NULL OR c.resolved=1)',
                    (mid, floor, cutoff_ns)).fetchall()]
                for seq in eligible:
                    self.db.execute('DELETE FROM conflicts WHERE delivery_id IN (SELECT delivery_id FROM deliveries WHERE sequence=?)', (seq,))
                    self.db.execute('DELETE FROM deliveries WHERE sequence=?', (seq,))
                removed += len(eligible)
                if eligible:
                    self._status(Status(mid, SourceState.INCOMPLETE, 'retained_history_trimmed', at))
        if removed or expired_meetings:
            self.db.execute('PRAGMA wal_checkpoint(PASSIVE)')
        return {'pruned_deliveries': removed, 'expired_meetings': expired_meetings}

    def compact(self) -> None:
        self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        self.db.execute('VACUUM')
        self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')


class DurableFakeConsumer:
    """Persist context and idempotency before separately acknowledging the source."""
    def __init__(self, path: Path, *, max_database_bytes=150_000_000):
        self.db = connect_private(Path(path), max_database_bytes=max_database_bytes)
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

    def forget_meeting(self, meeting_id: str) -> None:
        try:
            self.db.execute('BEGIN IMMEDIATE')
            for row in self.db.execute('SELECT delivery_id,body FROM accepted').fetchall():
                value = json.loads(row['body'])
                input_body = value.get('caption') or value.get('status') or {}
                if input_body.get('meeting_id') == meeting_id:
                    self.db.execute('DELETE FROM accepted WHERE delivery_id=?', (row['delivery_id'],))
            self.db.execute('DELETE FROM context WHERE meeting_id=?', (meeting_id,))
            self.db.execute('COMMIT')
            self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            self.db.execute('VACUUM')
            self.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        except sqlite3.Error:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise StorageError('consumer copy deletion failed; retry explicit deletion') from None

    def forget_deleted_meetings(self, identities: set[str]) -> None:
        meetings = {row['meeting_id'] for row in self.db.execute('SELECT meeting_id FROM context')}
        for row in self.db.execute('SELECT body FROM accepted'):
            value = json.loads(row['body'])
            mid = (value.get('caption') or value.get('status') or {}).get('meeting_id')
            if isinstance(mid, str):
                meetings.add(mid)
        for mid in meetings:
            if digest(mid) in identities:
                self.forget_meeting(mid)
