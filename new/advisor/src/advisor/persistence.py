"""Session domain persistence: schema, store and unit-of-work repositories.

Ported from legacy advisor adviser_schema/adviser_store/adviser_repositories
(2026-11-10), adapted per plan C: sessions replace meetings+local_sessions and
carry owner/meeting_no/event binding/stream token/lease; channels are local+im
only (no Feishu meeting channel); subscriptions per STATE_MACHINES.md.
"""
from contextlib import contextmanager
from collections.abc import Callable
import json
from pathlib import Path
import secrets
import sqlite3
import time
from uuid import NAMESPACE_URL, uuid4, uuid5

from .errors import StateError, StorageError
from .models import (Caption, JobState, MaterialState, ReplyState, SourceState,
                     caption_to_dict, digest)
from .sqlite import SQLiteDatabase

SCHEMA_VERSION = 1

SCHEMA = (
    '''CREATE TABLE sessions (
        session_id TEXT PRIMARY KEY, owner_open_id TEXT NOT NULL,
        meeting_no TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
        event_calendar_id TEXT NOT NULL, event_id TEXT NOT NULL,
        phase TEXT NOT NULL DEFAULT 'recording', stream_token TEXT NOT NULL,
        generation INTEGER NOT NULL DEFAULT 0, decided_generation INTEGER NOT NULL DEFAULT 0,
        error TEXT, retry_at REAL NOT NULL DEFAULT 0, cooldown_until REAL NOT NULL DEFAULT 0,
        material_version TEXT, material_state TEXT NOT NULL DEFAULT 'disabled',
        created_ns INTEGER NOT NULL, started_at REAL NOT NULL, ended_ns INTEGER,
        idle_timeout_s INTEGER NOT NULL DEFAULT 900,
        lease_owner TEXT, lease_expires_at REAL)''',
    'CREATE INDEX sessions_owner ON sessions(owner_open_id, phase)',
    'CREATE INDEX sessions_meeting ON sessions(meeting_no, phase)',
    '''CREATE TABLE inputs (
        delivery_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, sequence INTEGER NOT NULL,
        body TEXT NOT NULL, created_ns INTEGER NOT NULL)''',
    'CREATE INDEX input_session ON inputs(session_id,sequence)',
    '''CREATE TABLE context (
        session_id TEXT NOT NULL, sentence_key TEXT NOT NULL, body TEXT NOT NULL,
        suppressed INTEGER NOT NULL DEFAULT 0, generation INTEGER NOT NULL,
        PRIMARY KEY (session_id,sentence_key))''',
    '''CREATE TABLE jobs (
        job_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, generation INTEGER NOT NULL,
        snapshot TEXT NOT NULL, role_version TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending', result TEXT, created_ns INTEGER NOT NULL,
        context_version TEXT)''',
    'CREATE INDEX job_session ON jobs(session_id,generation)',
    '''CREATE TABLE outbox (
        uuid TEXT PRIMARY KEY, session_id TEXT NOT NULL, kind TEXT NOT NULL,
        content TEXT NOT NULL, evidence TEXT NOT NULL, generation INTEGER NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending', error TEXT, created_ns INTEGER NOT NULL,
        sent_at REAL, context_version TEXT, answer_uuid TEXT NOT NULL,
        channel TEXT NOT NULL, recipient_id TEXT)''',
    'CREATE INDEX outbox_answer ON outbox(answer_uuid)',
    'CREATE INDEX outbox_session ON outbox(session_id,state)',
    '''CREATE TABLE session_materials (
        session_id TEXT PRIMARY KEY, body TEXT NOT NULL, loaded_ns INTEGER NOT NULL)''',
    '''CREATE TABLE sources (
        session_id TEXT NOT NULL, source TEXT NOT NULL,
        acked_sequence INTEGER NOT NULL DEFAULT 0, last_sequence INTEGER,
        last_seen_ns INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'live',
        lost_from INTEGER, lost_to INTEGER, speaker TEXT,
        PRIMARY KEY (session_id,source))''',
    '''CREATE TABLE subscriptions (
        request_no INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
        subscriber_open_id TEXT NOT NULL, state TEXT NOT NULL,
        created_ns INTEGER NOT NULL, decided_ns INTEGER)''',
    'CREATE INDEX subscriptions_session ON subscriptions(session_id,state)',
    '''CREATE TABLE idempotency (
        key TEXT PRIMARY KEY, response TEXT NOT NULL, created_ns INTEGER NOT NULL)''',
)

SESSION_PHASES = ('recording', 'draining', 'ended')
SESSION_TRANSITIONS = {('recording', 'draining'), ('recording', 'ended'), ('draining', 'ended')}
SUBSCRIPTION_STATES = ('requested', 'approved', 'denied', 'revoked')


def encode(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def decode_mapping(raw):
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise StorageError('invalid stored mapping')
    return value


FaultHook = Callable[[str], None]


class JobRepository:
    def __init__(self, connection: sqlite3.Connection, checkpoint: FaultHook):
        self.connection = connection
        self.checkpoint = checkpoint

    def decision_state(self, job_id: str) -> JobState:
        row = self.connection.execute('SELECT state FROM jobs WHERE job_id=?', (job_id,)).fetchone()
        if row is None:
            raise StorageError('unknown adviser job')
        try:
            return JobState(row['state'])
        except ValueError:
            raise StorageError('invalid adviser job state') from None

    def abandon(self, job_id: str) -> None:
        changed = self.connection.execute(
            'UPDATE jobs SET state=? WHERE job_id=? AND state IN (?,?)',
            (JobState.ABANDONED, job_id, JobState.PENDING, JobState.RUNNING)).rowcount
        if changed != 1:
            raise StorageError('illegal adviser job transition')
        self.checkpoint('after_job_abandoned')

    def complete(self, job_id: str, *, result: str, snapshot: str) -> None:
        changed = self.connection.execute(
            'UPDATE jobs SET state=?,result=?,snapshot=? WHERE job_id=? AND state IN (?,?)',
            (JobState.COMPLETE, result, snapshot, job_id,
             JobState.PENDING, JobState.RUNNING)).rowcount
        if changed != 1:
            raise StorageError('illegal adviser job transition')
        self.checkpoint('after_job_completed')

    def claim(self, job_id: str) -> None:
        changed = self.connection.execute(
            'UPDATE jobs SET state=? WHERE job_id=? AND state=?',
            (JobState.RUNNING, job_id, JobState.PENDING)).rowcount
        if changed != 1:
            raise StorageError('adviser job is no longer pending')

    def retry(self, job_id: str) -> None:
        changed = self.connection.execute(
            'UPDATE jobs SET state=? WHERE job_id=? AND state=?',
            (JobState.PENDING, job_id, JobState.RUNNING)).rowcount
        if changed != 1:
            raise StorageError('adviser job is not running')

    def recover_running(self) -> None:
        self.connection.execute(
            'UPDATE jobs SET state=? WHERE state=?', (JobState.PENDING, JobState.RUNNING))


class SessionRepository:
    def __init__(self, connection: sqlite3.Connection, checkpoint: FaultHook):
        self.connection = connection
        self.checkpoint = checkpoint

    def require(self, session_id: str) -> dict:
        row = self.connection.execute(
            'SELECT * FROM sessions WHERE session_id=?', (session_id,)).fetchone()
        if row is None:
            raise StorageError('unknown adviser session')
        return dict(row)

    def complete_decision(self, session_id: str, *, generation: int) -> None:
        changed = self.connection.execute(
            '''UPDATE sessions SET decided_generation=MAX(decided_generation,?),
               error=NULL,retry_at=0 WHERE session_id=?''',
            (generation, session_id)).rowcount
        if changed != 1:
            raise StorageError('unknown adviser session')
        self.checkpoint('after_session_updated')

    def record_job_failure(self, session_id: str, *, reason: str, retry_at: float) -> None:
        changed = self.connection.execute(
            'UPDATE sessions SET error=?,retry_at=? WHERE session_id=?',
            (reason, retry_at, session_id)).rowcount
        if changed != 1:
            raise StorageError('unknown adviser session')

    def schedule_reply_recheck(self, session_id: str) -> None:
        changed = self.connection.execute(
            'UPDATE sessions SET generation=generation+1,retry_at=0 WHERE session_id=?',
            (session_id,)).rowcount
        if changed != 1:
            raise StorageError('unknown adviser session')

    def set_cooldown(self, session_id: str, until: float) -> None:
        changed = self.connection.execute(
            'UPDATE sessions SET cooldown_until=? WHERE session_id=?',
            (until, session_id)).rowcount
        if changed != 1:
            raise StorageError('unknown adviser session')


class ReplyOutboxRepository:
    def __init__(self, connection: sqlite3.Connection, checkpoint: FaultHook):
        self.connection = connection
        self.checkpoint = checkpoint

    def cancel_pending(self, session_id: str) -> None:
        self.connection.execute(
            "UPDATE outbox SET state=? WHERE session_id=? AND kind='reply' AND state=?",
            (ReplyState.CANCELLED, session_id, ReplyState.PENDING))
        self.checkpoint('after_old_replies_cancelled')

    def save_reply(self, *, message_uuid: str, session_id: str, content: str,
                   evidence: str, generation: int, created_ns: int,
                   context_version: str | None, recipients: tuple[str, ...],
                   now: float) -> None:
        """One local fact row (sent) + one pending IM row per recipient."""
        targets = [('local', None, message_uuid, ReplyState.SENT, now)] + [
            ('im', recipient, str(uuid5(NAMESPACE_URL, f'{message_uuid}:im:{recipient}')),
             ReplyState.PENDING, None)
            for recipient in recipients]
        for channel, recipient, delivery_uuid, state, sent_at in targets:
            self.connection.execute(
                '''INSERT INTO outbox(
                       uuid,session_id,kind,content,evidence,generation,created_ns,
                       context_version,answer_uuid,channel,recipient_id,state,sent_at)
                   VALUES (?,?,'reply',?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(uuid) DO UPDATE SET
                       generation=excluded.generation,
                       context_version=excluded.context_version,
                       state=CASE WHEN outbox.state=? THEN ? ELSE outbox.state END''',
                (delivery_uuid, session_id, content, evidence, generation, created_ns,
                 context_version, message_uuid, channel, recipient, state, sent_at,
                 ReplyState.CANCELLED, ReplyState.PENDING))
        self.checkpoint('after_reply_saved')

    def recover_sending(self) -> None:
        self.connection.execute(
            "UPDATE outbox SET state=?,error='sender_interrupted' WHERE state=?",
            (ReplyState.UNCERTAIN, ReplyState.SENDING))

    def resolve(self, session_id: str, message_uuid: str, outcome: str, *, now: float) -> dict:
        row = self.connection.execute(
            'SELECT * FROM outbox WHERE session_id=? AND uuid=?',
            (session_id, message_uuid)).fetchone()
        allowed = {ReplyState.UNCERTAIN, ReplyState.SENDING, ReplyState.REJECTED}
        try:
            state = ReplyState(row['state']) if row else None
        except ValueError:
            raise StorageError('invalid reply state') from None
        if state not in allowed:
            raise StorageError('reply is not an unresolved send; check replies first')
        target = {'sent': ReplyState.SENT, 'retry': ReplyState.PENDING,
                  'cancel': ReplyState.CANCELLED}[outcome]
        changed = self.connection.execute(
            'UPDATE outbox SET state=?,error=NULL,sent_at=? WHERE uuid=? AND state IN (?,?,?)',
            (target, now if outcome == 'sent' else None, message_uuid,
             ReplyState.UNCERTAIN, ReplyState.SENDING, ReplyState.REJECTED)).rowcount
        if changed != 1:
            raise StorageError('illegal reply transition')
        return dict(row)

    def claim_sending(self, message_uuid: str) -> None:
        changed = self.connection.execute(
            'UPDATE outbox SET state=? WHERE uuid=? AND state=?',
            (ReplyState.SENDING, message_uuid, ReplyState.PENDING)).rowcount
        if changed != 1:
            raise StorageError('reply is no longer pending')

    def confirm_sent(self, message_uuid: str, *, now: float) -> dict:
        row = self.connection.execute(
            'SELECT * FROM outbox WHERE uuid=?', (message_uuid,)).fetchone()
        if row is None:
            raise StorageError('reply send state mismatch')
        try:
            state = ReplyState(row['state'])
        except ValueError:
            raise StorageError('invalid reply state') from None
        if state != ReplyState.SENDING:
            raise StorageError('reply send state mismatch')
        changed = self.connection.execute(
            'UPDATE outbox SET state=?,sent_at=?,error=NULL WHERE uuid=? AND state=?',
            (ReplyState.SENT, now, message_uuid, ReplyState.SENDING)).rowcount
        if changed != 1:
            raise StorageError('illegal reply transition')
        return dict(row)

    def mark_error(self, message_uuid: str, reason: str, *, uncertain: bool) -> None:
        target = ReplyState.UNCERTAIN if uncertain else ReplyState.REJECTED
        changed = self.connection.execute(
            'UPDATE outbox SET state=?,error=? WHERE uuid=? AND state=?',
            (target, reason, message_uuid, ReplyState.SENDING)).rowcount
        if changed != 1:
            raise StorageError('reply send state mismatch')

    def cancel_pending_recipients(self, session_id: str, keep: set[str]) -> None:
        """Revoked destinations must never receive an old pending answer."""
        rows = self.connection.execute(
            "SELECT uuid,recipient_id FROM outbox WHERE session_id=? AND kind='reply' "
            "AND channel='im' AND state='pending'", (session_id,)).fetchall()
        for row in rows:
            if row['recipient_id'] not in keep:
                self.connection.execute(
                    "UPDATE outbox SET state='cancelled',error='destination_disabled' "
                    "WHERE uuid=?", (row['uuid'],))


class SubscriptionRepository:
    def __init__(self, connection: sqlite3.Connection, checkpoint: FaultHook):
        self.connection = connection
        self.checkpoint = checkpoint

    def row(self, request_no: int) -> dict | None:
        row = self.connection.execute(
            'SELECT * FROM subscriptions WHERE request_no=?', (request_no,)).fetchone()
        return dict(row) if row else None

    def require(self, request_no: int) -> dict:
        row = self.row(request_no)
        if row is None:
            raise StorageError('unknown subscription request')
        return row

    def insert(self, session_id: str, subscriber_open_id: str, *, now: float) -> dict:
        cursor = self.connection.execute(
            'INSERT INTO subscriptions(session_id,subscriber_open_id,state,created_ns) '
            "VALUES (?,?,'requested',?)",
            (session_id, subscriber_open_id, int(now * 1_000_000_000)))
        lastrowid = cursor.lastrowid
        if lastrowid is None:
            raise StorageError('subscription insert failed')
        return self.require(lastrowid)

    def settle(self, request_no: int, state: str, *, now: float,
               expected: str = 'requested') -> dict:
        changed = self.connection.execute(
            'UPDATE subscriptions SET state=?,decided_ns=? WHERE request_no=? AND state=?',
            (state, int(now * 1_000_000_000), request_no, expected)).rowcount
        if changed != 1:
            raise StateError('invalid_state')
        self.checkpoint('after_subscription_decided')
        return self.require(request_no)

    def latest_for(self, session_id: str, subscriber_open_id: str) -> dict | None:
        row = self.connection.execute(
            'SELECT * FROM subscriptions WHERE session_id=? AND subscriber_open_id=? '
            'ORDER BY request_no DESC LIMIT 1', (session_id, subscriber_open_id)).fetchone()
        return dict(row) if row else None

    def approved_recipients(self, session_id: str, owner_open_id: str) -> tuple[str, ...]:
        rows = self.connection.execute(
            "SELECT subscriber_open_id FROM subscriptions WHERE session_id=? AND state='approved'",
            (session_id,)).fetchall()
        recipients = {row['subscriber_open_id'] for row in rows}
        recipients.add(owner_open_id)  # 组织者行：建会即写入的等价物
        return tuple(sorted(recipients))


class UnitOfWork:
    """One transaction shared by repositories; repositories never commit."""

    def __init__(self, connection: sqlite3.Connection, *, capacity_check: Callable[[], None],
                 fault: FaultHook | None = None, lock=None):
        self.connection = connection
        self.capacity_check = capacity_check
        self._lock = lock
        self._fault = fault or (lambda _stage: None)
        self.jobs = JobRepository(connection, self._fault)
        self.sessions = SessionRepository(connection, self._fault)
        self.outbox = ReplyOutboxRepository(connection, self._fault)
        self.subscriptions = SubscriptionRepository(connection, self._fault)

    def __enter__(self):
        if self._lock is not None:
            self._lock.acquire()
        try:
            self.connection.execute('BEGIN IMMEDIATE')
        except BaseException:
            if self._lock is not None:
                self._lock.release()
            raise
        return self

    def __exit__(self, exc_type, exc, traceback):
        try:
            if exc_type is not None:
                if self.connection.in_transaction:
                    self.connection.execute('ROLLBACK')
                return False
            try:
                self.capacity_check()
                self.connection.execute('COMMIT')
            except (Exception, KeyboardInterrupt):
                if self.connection.in_transaction:
                    self.connection.execute('ROLLBACK')
                raise
            return False
        finally:
            if self._lock is not None:
                self._lock.release()
