"""Adviser repositories sharing one caller-owned SQLite unit of work."""
from collections.abc import Callable
import sqlite3

from ...models import JobState, ParticipationState, ReplyState
from .sqlite import StorageError


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
            (JobState.ABANDONED, job_id, JobState.PENDING, JobState.RUNNING),
        ).rowcount
        if changed != 1:
            raise StorageError('illegal adviser job transition')
        self.checkpoint('after_job_abandoned')

    def complete(self, job_id: str, *, result: str, snapshot: str) -> None:
        changed = self.connection.execute(
            'UPDATE jobs SET state=?,result=?,snapshot=? WHERE job_id=? AND state IN (?,?)',
            (JobState.COMPLETE, result, snapshot, job_id, JobState.PENDING, JobState.RUNNING),
        ).rowcount
        if changed != 1:
            raise StorageError('illegal adviser job transition')
        self.checkpoint('after_job_completed')

    def claim(self, job_id: str) -> None:
        changed = self.connection.execute(
            'UPDATE jobs SET state=? WHERE job_id=? AND state=?',
            (JobState.RUNNING, job_id, JobState.PENDING),
        ).rowcount
        if changed != 1:
            raise StorageError('adviser job is no longer pending')

    def retry(self, job_id: str) -> None:
        changed = self.connection.execute(
            'UPDATE jobs SET state=? WHERE job_id=? AND state=?',
            (JobState.PENDING, job_id, JobState.RUNNING),
        ).rowcount
        if changed != 1:
            raise StorageError('adviser job is not running')

    def recover_running(self) -> None:
        self.connection.execute(
            'UPDATE jobs SET state=? WHERE state=?',
            (JobState.PENDING, JobState.RUNNING),
        )


class MeetingRepository:
    def __init__(self, connection: sqlite3.Connection, checkpoint: FaultHook):
        self.connection = connection
        self.checkpoint = checkpoint

    def require(self, meeting_id: str) -> dict:
        row = self.connection.execute(
            'SELECT * FROM meetings WHERE meeting_id=?', (meeting_id,)).fetchone()
        if row is None:
            raise StorageError('unknown adviser meeting')
        value = dict(row)
        try:
            ParticipationState(value['participation'])
        except ValueError:
            raise StorageError('invalid adviser participation state') from None
        return value

    def complete_decision(self, meeting_id: str, *, generation: int,
                          participation: ParticipationState) -> None:
        changed = self.connection.execute(
            '''UPDATE meetings SET decided_generation=MAX(decided_generation,?),
               participation=?,error=NULL,retry_at=0 WHERE meeting_id=?''',
            (generation, participation, meeting_id),
        ).rowcount
        if changed != 1:
            raise StorageError('unknown adviser meeting')
        self.checkpoint('after_meeting_updated')

    def record_job_failure(self, meeting_id: str, *, reason: str, retry_at: float) -> None:
        changed = self.connection.execute(
            'UPDATE meetings SET error=?,retry_at=? WHERE meeting_id=?',
            (reason, retry_at, meeting_id),
        ).rowcount
        if changed != 1:
            raise StorageError('unknown adviser meeting')

    def schedule_reply_recheck(self, meeting_id: str) -> None:
        changed = self.connection.execute(
            'UPDATE meetings SET generation=generation+1,retry_at=0 WHERE meeting_id=?',
            (meeting_id,),
        ).rowcount
        if changed != 1:
            raise StorageError('unknown adviser meeting')

    def set_cooldown(self, meeting_id: str, until: float) -> None:
        changed = self.connection.execute(
            'UPDATE meetings SET cooldown_until=? WHERE meeting_id=?', (until, meeting_id)).rowcount
        if changed != 1:
            raise StorageError('unknown adviser meeting')


class ReplyOutboxRepository:
    def __init__(self, connection: sqlite3.Connection, checkpoint: FaultHook):
        self.connection = connection
        self.checkpoint = checkpoint

    def cancel_pending(self, meeting_id: str) -> None:
        self.connection.execute(
            'UPDATE outbox SET state=? WHERE meeting_id=? AND kind=\'reply\' AND state=?',
            (ReplyState.CANCELLED, meeting_id, ReplyState.PENDING),
        )
        self.checkpoint('after_old_replies_cancelled')

    def save_reply(self, *, message_uuid: str, meeting_id: str, content: str,
                   evidence: str, generation: int, created_ns: int,
                   context_version: str | None) -> None:
        self.connection.execute(
            '''INSERT INTO outbox(
                   uuid,meeting_id,kind,content,evidence,generation,created_ns,context_version)
               VALUES (?,?,'reply',?,?,?,?,?)
               ON CONFLICT(uuid) DO UPDATE SET
                   generation=excluded.generation,
                   context_version=excluded.context_version,
                   state=CASE WHEN outbox.state=? THEN ? ELSE outbox.state END''',
            (message_uuid, meeting_id, content, evidence, generation, created_ns, context_version,
             ReplyState.CANCELLED, ReplyState.PENDING),
        )
        self.checkpoint('after_reply_saved')

    def recover_sending(self) -> None:
        self.connection.execute(
            'UPDATE outbox SET state=?,error=\'sender_interrupted\' WHERE state=?',
            (ReplyState.UNCERTAIN, ReplyState.SENDING),
        )

    def resolve(self, meeting_id: str, message_uuid: str, outcome: str, *, now: float) -> dict:
        row = self.connection.execute(
            'SELECT * FROM outbox WHERE meeting_id=? AND uuid=?',
            (meeting_id, message_uuid),
        ).fetchone()
        allowed = {ReplyState.UNCERTAIN, ReplyState.SENDING, ReplyState.REJECTED}
        try:
            state = ReplyState(row['state']) if row else None
        except ValueError:
            raise StorageError('invalid reply state') from None
        if state not in allowed:
            raise StorageError('reply is not an unresolved send; check replies first')
        target = {
            'sent': ReplyState.SENT,
            'retry': ReplyState.PENDING,
            'cancel': ReplyState.CANCELLED,
        }[outcome]
        changed = self.connection.execute(
            'UPDATE outbox SET state=?,error=NULL,sent_at=? WHERE uuid=? AND state IN (?,?,?)',
            (target, now if outcome == 'sent' else None, message_uuid,
             ReplyState.UNCERTAIN, ReplyState.SENDING, ReplyState.REJECTED),
        ).rowcount
        if changed != 1:
            raise StorageError('illegal reply transition')
        return dict(row)

    def claim_sending(self, message_uuid: str) -> None:
        changed = self.connection.execute(
            'UPDATE outbox SET state=? WHERE uuid=? AND state=?',
            (ReplyState.SENDING, message_uuid, ReplyState.PENDING),
        ).rowcount
        if changed != 1:
            raise StorageError('reply is no longer pending')

    def confirm_sent(self, message_uuid: str, *, now: float) -> dict:
        row = self.connection.execute('SELECT * FROM outbox WHERE uuid=?', (message_uuid,)).fetchone()
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
            (ReplyState.SENT, now, message_uuid, ReplyState.SENDING),
        ).rowcount
        if changed != 1:
            raise StorageError('illegal reply transition')
        return dict(row)

    def mark_error(self, message_uuid: str, reason: str, *, uncertain: bool) -> None:
        target = ReplyState.UNCERTAIN if uncertain else ReplyState.REJECTED
        changed = self.connection.execute(
            'UPDATE outbox SET state=?,error=? WHERE uuid=? AND state=?',
            (target, reason, message_uuid, ReplyState.SENDING),
        ).rowcount
        if changed != 1:
            raise StorageError('reply send state mismatch')


class AdviserUnitOfWork:
    """One transaction shared by repositories; repositories never commit."""

    def __init__(self, connection: sqlite3.Connection, *, capacity_check: Callable[[], None],
                 fault: FaultHook | None = None):
        self.connection = connection
        self.capacity_check = capacity_check
        self._fault = fault or (lambda _stage: None)
        self.jobs = JobRepository(connection, self._fault)
        self.meetings = MeetingRepository(connection, self._fault)
        self.outbox = ReplyOutboxRepository(connection, self._fault)

    def __enter__(self):
        self.connection.execute('BEGIN IMMEDIATE')
        return self

    def __exit__(self, exc_type, exc, traceback):
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
