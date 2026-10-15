"""Atomic page ingestion and independent durable consumer progress on local SQLite."""
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
import json
import sqlite3
import time
from typing import Callable

from .models import Caption, ChangeKind, Delivery, IngestMode, SourceState, Status, digest, epoch_ns
from .adapters.persistence.sqlite import CapacityError, SQLiteDatabase, StorageError, connect_private, private_directory
from .adapters.persistence.source_migrations import migrate_source, require_current_source_schema


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


def caption_from_json(body: str) -> Caption:
    value = decode_mapping(body)
    try:
        value['mode'] = IngestMode(value['mode'])
        return Caption(**value)
    except (ValueError, TypeError, KeyError):
        raise StorageError('invalid stored caption; body hidden') from None


class Store:
    def __init__(self, path: Path, *, max_payload_bytes: int = 100_000_000, max_database_bytes: int = 150_000_000):
        self.path = Path(path)
        self.max_payload_bytes = max_payload_bytes
        self.opened_ns = time.time_ns()
        self.max_database_bytes = max_database_bytes
        self.last_commit_captions = []
        self.last_commit_changes = []
        self.db = connect_private(self.path, max_database_bytes=max_database_bytes)
        try:
            migrate_source(self.path, self.db, max_database_bytes)
        except BaseException:
            self.close()
            raise

    @classmethod
    def open_readonly(cls, path: Path, *, max_payload_bytes: int = 100_000_000,
                      max_database_bytes: int = 150_000_000):
        instance = cls.__new__(cls)
        instance.path = Path(path)
        instance.max_payload_bytes = max_payload_bytes
        instance.opened_ns = time.time_ns()
        instance.max_database_bytes = max_database_bytes
        instance.last_commit_captions = []
        instance.last_commit_changes = []
        instance.db = SQLiteDatabase.open_readonly(
            instance.path, max_database_bytes=max_database_bytes).connection
        try:
            require_current_source_schema(instance.db)
        except BaseException:
            instance.db.close()
            raise
        return instance

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
        return decode_mapping(row['checkpoint'])

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
            self.db.execute('INSERT OR IGNORE INTO risks(meeting_id,identity,body) VALUES (?,?,?)',
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
        value = decode_mapping(row['state'])
        try:
            value['state'] = SourceState(value['state'])
            return Status(**value)
        except (ValueError,TypeError,KeyError):
            raise StorageError('invalid stored source status; body hidden') from None

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
        changes = []
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
                changes.append((kind, candidate))
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
        self.last_commit_changes = changes
        return counts

    def session(self, app_key: str) -> dict | None:
        row = self.db.execute('SELECT body FROM sessions WHERE app_key=?', (app_key,)).fetchone()
        return decode_mapping(row['body']) if row else None

    def _record_membership(self, session: dict) -> None:
        mid = session.get('meeting_id')
        phase = session.get('phase')
        if not mid or phase not in ('joined', 'left', 'leave_uncertain'):
            return
        self.db.execute('INSERT INTO memberships(meeting_id,active,left_at) VALUES (?,?,?) ON CONFLICT(meeting_id) '
                        'DO UPDATE SET active=excluded.active,left_at=excluded.left_at',
                        (mid, int(phase != 'left'), session.get('left_at') or session.get('manual_confirmation_at')))

    def save_session(self, app_key: str, session: dict) -> None:
        with self.transaction():
            self._record_membership(session)
            self.db.execute('INSERT INTO sessions(app_key,body) VALUES (?,?) ON CONFLICT(app_key) DO UPDATE SET body=excluded.body',
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
                body = decode_mapping(row['body'])
                value = {'sequence': row['sequence'], 'delivery_id': row['delivery_id'], 'kind': row['kind'],
                         'caption': body if row['kind'] != ChangeKind.STATUS else None,
                         'status': body if row['kind'] == ChangeKind.STATUS else None,
                         'replayed': row['attempt_count'] > 0 or row['created_ns'] < self.opened_ns}
                try:
                    result.append(Delivery.from_dict(value))
                except (ValueError,TypeError,KeyError):
                    raise StorageError('invalid stored delivery; body hidden, not acknowledged') from None
                self.db.execute('INSERT INTO attempts(meeting_id,consumer_id,sequence,count) VALUES (?,?,?,1) ON CONFLICT(meeting_id,consumer_id,sequence) '
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
        self.db.execute('INSERT OR IGNORE INTO tombstones(identity,deleted_at) VALUES (?,?)', (digest(meeting_id), at))

    def forget_meeting(self, meeting_id: str, *, at: str) -> None:
        # Destructive explicit operation: bypass content quota so full storage can be cleaned.
        with self.transaction(check_capacity=False):
            self._forget_meeting(meeting_id, at=at)
        self.compact()

    def prune(self, cutoff_ns: int, *, at: str, protected_meetings=frozenset()) -> dict:
        removed = expired_meetings = 0
        with self.transaction(check_capacity=False):
            inactive = {}
            for row in self.db.execute('SELECT meeting_id,left_at FROM memberships WHERE active=0').fetchall():
                left_at = epoch_ns(row['left_at'])
                if left_at is not None and left_at < cutoff_ns:
                    inactive[row['meeting_id']] = True
            for meeting in self.db.execute('SELECT meeting_id FROM meetings').fetchall():
                mid = meeting['meeting_id']
                if mid in protected_meetings:
                    continue
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
            CREATE TABLE IF NOT EXISTS rule_replies (uuid TEXT PRIMARY KEY, meeting_id TEXT NOT NULL,
                                                     content TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0);
        ''')

    def accept(self, delivery: Delivery, *, reply: tuple[str, str] | None = None) -> bool:
        try:
            self.db.execute('BEGIN IMMEDIATE')
            inserted = self.db.execute('INSERT OR IGNORE INTO accepted(delivery_id,body) VALUES (?,?)',
                                       (delivery.delivery_id, encode(delivery.to_dict()))).rowcount
            if inserted and delivery.caption and delivery.kind in (ChangeKind.ADDED, ChangeKind.UPDATED):
                caption = delivery.caption
                self.db.execute('INSERT INTO context(meeting_id,sentence_key,body) VALUES (?,?,?) ON CONFLICT(meeting_id,sentence_key) DO UPDATE SET body=excluded.body',
                                (caption.meeting_id, caption.sentence_key, encode(asdict(caption))))
            if reply is not None and delivery.caption and delivery.kind in (ChangeKind.ADDED, ChangeKind.UPDATED):
                self.db.execute('INSERT OR IGNORE INTO rule_replies(uuid,meeting_id,content) VALUES (?,?,?)',
                                (reply[0], delivery.caption.meeting_id, reply[1]))
            self.db.execute('COMMIT')
            return bool(inserted)
        except sqlite3.Error:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise StorageError('fake consumer persistence failed; input not acknowledged') from None

    def pending_reply(self, message_uuid: str):
        return self.db.execute('SELECT meeting_id,content FROM rule_replies WHERE uuid=? AND sent=0',
                               (message_uuid,)).fetchone()

    def mark_reply_sent(self, message_uuid: str) -> None:
        try:
            self.db.execute('UPDATE rule_replies SET sent=1 WHERE uuid=?', (message_uuid,))
        except sqlite3.Error:
            raise StorageError('reply confirmation persistence failed; check meeting UI before retrying consume') from None

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
            self.db.execute('DELETE FROM rule_replies WHERE meeting_id=?', (meeting_id,))
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
        meetings.update(row['meeting_id'] for row in self.db.execute('SELECT meeting_id FROM rule_replies'))
        for row in self.db.execute('SELECT body FROM accepted'):
            value = json.loads(row['body'])
            mid = (value.get('caption') or value.get('status') or {}).get('meeting_id')
            if isinstance(mid, str):
                meetings.add(mid)
        for mid in meetings:
            if digest(mid) in identities:
                self.forget_meeting(mid)
