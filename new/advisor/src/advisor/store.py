"""SessionStore: session lifecycle, ingest, jobs, outbox, subscriptions, leases.

Facade over persistence.py repositories; ports the legacy AdviserStore surface
used by the supervisor, with STATE_MACHINES.md semantics (no paused phase,
revoked terminal, draining/ended reject new subscriptions and late results).
"""
from pathlib import Path
import secrets
import time
from uuid import uuid4

from .errors import StateError, StorageError
from .models import (Caption, JobState, MaterialState, ReplyState, SourceState,
                     caption_from_json, caption_to_dict, digest)
from .persistence import (SCHEMA, SCHEMA_VERSION, SESSION_PHASES, SESSION_TRANSITIONS,
                          UnitOfWork, decode_mapping, encode)
from .snapshot import SnapshotBuilder, is_reply_evidence_caption
from .decision import DecisionApplier
from .sqlite import SQLiteDatabase


def _tables(db) -> set[str]:
    return {row['name'] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def require_schema(connection) -> None:
    version = connection.execute('PRAGMA user_version').fetchone()[0]
    tables = _tables(connection) - {'sqlite_sequence'}  # appears lazily with AUTOINCREMENT
    if version != SCHEMA_VERSION or tables != {
            'sessions', 'inputs', 'context', 'jobs', 'outbox', 'session_materials',
            'sources', 'subscriptions', 'idempotency'}:
        raise StorageError('adviser database schema is unsupported; reinitialize')


def initialize_schema(database: SQLiteDatabase) -> None:
    with database.transaction():
        db = database.connection
        if db.execute('PRAGMA user_version').fetchone()[0] or _tables(db) - {'sqlite_sequence'}:
            require_schema(db)
            return
        for statement in SCHEMA:
            db.execute(statement)
        db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
        require_schema(db)


class SessionStore:
    def __init__(self, path: Path, *, max_database_bytes=150_000_000,
                 max_payload_bytes=100_000_000):
        self.path = Path(path)
        self.max_payload_bytes = max_payload_bytes
        database = SQLiteDatabase.open_current(self.path, max_database_bytes=max_database_bytes)
        self.db = database.connection
        try:
            require_schema(self.db)
        except BaseException:
            self.db.close()
            raise

    @classmethod
    def initialize(cls, path: Path, *, max_database_bytes=150_000_000) -> None:
        database = SQLiteDatabase.initialize(path, max_database_bytes=max_database_bytes)
        try:
            initialize_schema(database)
        finally:
            database.close()

    def close(self):
        self.db.close()

    def _check_capacity(self):
        if self.payload_bytes() > self.max_payload_bytes:
            raise StorageError('adviser content capacity exceeded')

    def payload_bytes(self):
        return sum(self.db.execute(
            f'SELECT COALESCE(SUM(length(CAST({column} AS BLOB))),0) FROM {table}').fetchone()[0]
            for table, column in (('inputs', 'body'), ('context', 'body'),
                                  ('jobs', 'snapshot'), ('jobs', 'result'),
                                  ('outbox', 'content'), ('outbox', 'evidence'),
                                  ('session_materials', 'body')))

    def unit_of_work(self, *, fault=None) -> UnitOfWork:
        return UnitOfWork(self.db, capacity_check=self._check_capacity, fault=fault)

    # -- sessions ---------------------------------------------------------

    def create_session(self, *, owner_open_id, meeting_no, title, event_calendar_id,
                       event_id, description='', idle_timeout_s=900, now=None):
        moment = time.time() if now is None else now
        session_id = 's-' + uuid4().hex
        stream_token = 'st-' + secrets.token_urlsafe(32)
        description = description or ''
        material = {'state': MaterialState.READY.value if description else MaterialState.DISABLED.value,
                    'context_version': digest(['material-v1', description]) if description else None,
                    'description': description, 'documents': []}
        with self.unit_of_work() as tx:
            active = tx.connection.execute(
                "SELECT session_id FROM sessions WHERE owner_open_id=? AND meeting_no=? "
                "AND phase IN ('recording','draining')", (owner_open_id, meeting_no)).fetchone()
            if active is not None:
                raise StateError('session already active for this meeting')
            tx.connection.execute(
                '''INSERT INTO sessions(session_id,owner_open_id,meeting_no,title,
                     event_calendar_id,event_id,phase,stream_token,material_version,
                     material_state,created_ns,started_at,idle_timeout_s)
                   VALUES (?,?,?,?,?,?, 'recording', ?, ?, ?, ?, ?, ?)''',
                (session_id, owner_open_id, meeting_no, title[:200], event_calendar_id,
                 event_id, stream_token, material['context_version'], material['state'],
                 int(moment * 1_000_000_000), moment, idle_timeout_s))
            tx.connection.execute(
                'INSERT INTO session_materials(session_id,body,loaded_ns) VALUES (?,?,?)',
                (session_id, encode(material), int(moment * 1_000_000_000)))
        return self.session(session_id)

    def session(self, session_id):
        row = self.db.execute('SELECT * FROM sessions WHERE session_id=?', (session_id,)).fetchone()
        if row is None:
            return None
        value = dict(row)
        value.pop('stream_token', None)  # token never leaves the store by accident
        return value

    def require_session(self, session_id):
        value = self.session(session_id)
        if value is None:
            raise StorageError('unknown adviser session')
        return value

    def session_token(self, session_id):
        row = self.db.execute(
            'SELECT stream_token,phase FROM sessions WHERE session_id=?', (session_id,)).fetchone()
        return dict(row) if row else None

    def list_sessions(self, owner_open_id=None):
        query = ("SELECT * FROM sessions WHERE phase IN ('recording','draining')"
                 " ORDER BY started_at DESC")
        params = ()
        if owner_open_id is not None:
            query = query.replace('WHERE phase', 'WHERE owner_open_id=? AND phase')
            params = (owner_open_id,)
        return [dict(row) for row in self.db.execute(query, params)]

    def set_phase(self, session_id, phase, *, now=None):
        if phase not in SESSION_PHASES:
            raise StorageError('invalid session phase')
        moment = time.time() if now is None else now
        with self.unit_of_work() as tx:
            row = tx.connection.execute(
                'SELECT phase FROM sessions WHERE session_id=?', (session_id,)).fetchone()
            if row is None:
                raise StorageError('unknown adviser session')
            if (row['phase'], phase) not in SESSION_TRANSITIONS:
                raise StateError(f'illegal session phase transition {row["phase"]} -> {phase}')
            tx.connection.execute(
                'UPDATE sessions SET phase=?,ended_ns=? WHERE session_id=?',
                (phase, int(moment * 1_000_000_000) if phase == 'ended' else None, session_id))
        return self.session(session_id)

    def active_by_meeting_no(self, meeting_no):
        """S3 注册表：正被服务中（recording）的会话。"""
        row = self.db.execute(
            "SELECT * FROM sessions WHERE meeting_no=? AND phase='recording' "
            'ORDER BY started_at DESC LIMIT 1', (meeting_no,)).fetchone()
        return dict(row) if row else None

    # -- leases (DB 内租约，替代旧 FileLock) --------------------------------

    def acquire_lease(self, session_id, owner, *, ttl_s=30, now=None):
        moment = time.time() if now is None else now
        with self.unit_of_work() as tx:
            changed = tx.connection.execute(
                '''UPDATE sessions SET lease_owner=?,lease_expires_at=?
                   WHERE session_id=? AND (lease_owner IS NULL OR lease_expires_at<? OR lease_owner=?)''',
                (owner, moment + ttl_s, session_id, moment, owner)).rowcount
            if changed != 1:
                row = tx.connection.execute(
                    'SELECT lease_owner FROM sessions WHERE session_id=?', (session_id,)).fetchone()
                if row is None:
                    raise StorageError('unknown adviser session')
                return False  # 同会话第二消费者被拒
        return True

    def release_lease(self, session_id, owner):
        with self.unit_of_work() as tx:
            tx.connection.execute(
                'UPDATE sessions SET lease_owner=NULL,lease_expires_at=NULL '
                'WHERE session_id=? AND lease_owner=?', (session_id, owner))

    # -- sources (ingest 侧维护) -------------------------------------------

    def ensure_source(self, session_id, source, *, now=None):
        moment = time.time() if now is None else now
        with self.unit_of_work() as tx:
            tx.connection.execute(
                'INSERT OR IGNORE INTO sources(session_id,source,last_seen_ns) VALUES (?,?,?)',
                (session_id, source, int(moment * 1_000_000_000)))

    def source_row(self, session_id, source):
        row = self.db.execute(
            'SELECT * FROM sources WHERE session_id=? AND source=?', (session_id, source)).fetchone()
        return dict(row) if row else None

    def source_ack(self, session_id, source, sequence, *, now=None):
        """推进持久化游标（ack 语义）；返回是否推进。"""
        moment = time.time() if now is None else now
        with self.unit_of_work() as tx:
            row = tx.connection.execute(
                'SELECT acked_sequence FROM sources WHERE session_id=? AND source=?',
                (session_id, source)).fetchone()
            if row is None:
                raise StorageError('unknown source')
            if sequence <= row['acked_sequence']:
                return False
            tx.connection.execute(
                'UPDATE sources SET acked_sequence=?,last_seen_ns=? WHERE session_id=? AND source=?',
                (sequence, int(moment * 1_000_000_000), session_id, source))
        return True

    def mark_source_drained(self, session_id, source, last_sequence):
        with self.unit_of_work() as tx:
            tx.connection.execute(
                "UPDATE sources SET state='drained',last_sequence=? "
                'WHERE session_id=? AND source=?', (last_sequence, session_id, source))

    def mark_source_lost(self, session_id, source, *, lost_from=None, lost_to=None):
        with self.unit_of_work() as tx:
            tx.connection.execute(
                "UPDATE sources SET state='lost',lost_from=?,lost_to=? "
                'WHERE session_id=? AND source=?', (lost_from, lost_to, session_id, source))

    def sources_ready_for_drain(self, session_id):
        rows = self.db.execute(
            'SELECT state FROM sources WHERE session_id=?', (session_id,)).fetchall()
        if not rows:
            return True  # 从未连接：由会话空闲超时兜底（视为可排空）
        return all(row['state'] in (SourceState.DRAINED.value, SourceState.LOST.value)
                   for row in rows)

    # -- ingest (STT final captions) ---------------------------------------

    def ingest_caption(self, session_id, caption: Caption, *, delivery_id=None, now=None):
        """Idempotent caption input; returns True when newly inserted."""
        if caption.meeting_id != session_id:
            raise StorageError('adviser input session mismatch')
        delivery_id = delivery_id or f'{session_id}:{caption.sentence_key}'
        moment = time.time() if now is None else now
        with self.unit_of_work() as tx:
            session = tx.sessions.require(session_id)
            if session['phase'] != 'recording':
                raise StateError('session_not_recording')
            added = tx.connection.execute(
                'INSERT OR IGNORE INTO inputs(delivery_id,session_id,sequence,body,created_ns) '
                'VALUES (?,?,?,?,?)',
                (delivery_id, session_id, session['generation'] + 1,
                 encode(caption_to_dict(caption)), int(moment * 1_000_000_000))).rowcount
            if not added:
                return False
            generation = session['generation'] + 1
            tx.connection.execute(
                'INSERT INTO context(session_id,sentence_key,body,suppressed,generation) '
                'VALUES (?,?,?,0,?) ON CONFLICT(session_id,sentence_key) DO UPDATE SET '
                'body=excluded.body,suppressed=0,generation=excluded.generation',
                (session_id, caption.sentence_key, encode(caption_to_dict(caption)), generation))
            tx.connection.execute(
                'UPDATE sessions SET generation=? WHERE session_id=?', (generation, session_id))
        return True

    def context_rows(self, session_id, *, limit):
        return [dict(row) for row in self.db.execute(
            'SELECT * FROM context WHERE session_id=? ORDER BY generation DESC LIMIT ?',
            (session_id, limit))]

    def context_count(self, session_id):
        return self.db.execute(
            'SELECT count(*) FROM context WHERE session_id=?', (session_id,)).fetchone()[0]

    def context_row(self, session_id, sentence_key):
        row = self.db.execute(
            'SELECT body,suppressed FROM context WHERE session_id=? AND sentence_key=?',
            (session_id, sentence_key)).fetchone()
        return dict(row) if row else None

    def materials(self, session_id):
        row = self.db.execute(
            'SELECT body FROM session_materials WHERE session_id=?', (session_id,)).fetchone()
        return decode_mapping(row['body']) if row else None

    # -- snapshot / jobs / decisions ----------------------------------------

    def snapshot(self, session_id, settings, *, now):
        session = self.require_session(session_id)
        return SnapshotBuilder(self).build(session_id, settings, now=now,
                                           joined_at=session['started_at'])

    def pending_reply_row(self, session_id):
        row = self.db.execute(
            "SELECT * FROM outbox WHERE session_id=? AND kind='reply' AND state='pending' "
            "AND channel='im' ORDER BY created_ns DESC LIMIT 1", (session_id,)).fetchone()
        return dict(row) if row else None

    def recent_reply_rows(self, session_id, *, limit):
        return [dict(row) for row in self.db.execute(
            "SELECT content,state FROM outbox WHERE session_id=? "
            "AND state IN ('sent','uncertain') GROUP BY answer_uuid "
            'ORDER BY created_ns DESC LIMIT ?', (session_id, limit))]

    def prepare_job(self, session_id, settings, role_version, *, now):
        session = self.require_session(session_id)
        if (session['generation'] <= session['decided_generation']
                or session['retry_at'] > now):
            return None
        snapshot = self.snapshot(session_id, settings, now=now)
        if not snapshot['fresh_input_keys'] and snapshot['pending_reply'] is None:
            with self.unit_of_work() as tx:
                for old in tx.connection.execute(
                        "SELECT job_id,snapshot FROM jobs WHERE session_id=? "
                        "AND state IN ('pending','running')", (session_id,)).fetchall():
                    tx.connection.execute(
                        "UPDATE jobs SET state='abandoned',snapshot=? WHERE job_id=?",
                        (encode({'snapshot_hash': digest(decode_mapping(old['snapshot']))}),
                         old['job_id']))
                tx.connection.execute(
                    'UPDATE sessions SET decided_generation=generation,error=NULL,retry_at=0 '
                    'WHERE session_id=?', (session_id,))
            return None
        job_id = digest([session_id, snapshot['generation'], role_version])
        if session['material_version']:
            job_id = digest([job_id, session['material_version']])
        with self.unit_of_work() as tx:
            current = tx.sessions.require(session_id)
            if current['phase'] not in ('recording', 'draining'):
                return None
            for old in tx.connection.execute(
                    "SELECT job_id,snapshot FROM jobs WHERE session_id=? AND state='pending' "
                    'AND generation<?', (session_id, snapshot['generation'])).fetchall():
                tx.connection.execute(
                    "UPDATE jobs SET state='abandoned',snapshot=? WHERE job_id=?",
                    (encode({'snapshot_hash': digest(decode_mapping(old['snapshot']))}),
                     old['job_id']))
            tx.connection.execute(
                'INSERT OR IGNORE INTO jobs(job_id,session_id,generation,snapshot,role_version,'
                'created_ns,context_version) VALUES (?,?,?,?,?,?,?)',
                (job_id, session_id, snapshot['generation'], encode(snapshot), role_version,
                 int(now * 1_000_000_000), current['material_version']))
            tx.connection.execute(
                "UPDATE jobs SET snapshot=? WHERE job_id=? AND state='pending'",
                (encode(snapshot), job_id))
        row = self.db.execute('SELECT * FROM jobs WHERE job_id=?', (job_id,)).fetchone()
        return dict(row) if row else None

    def finish_job(self, job, decision, *, now=None, settings=None):
        return DecisionApplier(self).apply(job, decision, now=now, settings=settings)

    def fail_job(self, job, reason, *, retry_at, now=None):
        moment = time.time() if now is None else now
        with self.unit_of_work() as tx:
            tx.jobs.retry(job['job_id'])
            tx.sessions.record_job_failure(job['session_id'], reason=reason, retry_at=retry_at)

    def mark_job_running(self, job_id):
        with self.unit_of_work() as tx:
            tx.jobs.claim(job_id)

    def recover(self):
        with self.unit_of_work() as tx:
            tx.outbox.recover_sending()
            tx.jobs.recover_running()

    def inflight_jobs(self, session_id):
        return self.db.execute(
            "SELECT count(*) FROM jobs WHERE session_id=? AND state IN ('pending','running')",
            (session_id,)).fetchone()[0]

    # -- outbox transport surface -------------------------------------------

    def next_reply(self, session_id, *, now):
        session = self.require_session(session_id)
        row = self.db.execute(
            '''SELECT * FROM outbox AS candidate
               WHERE session_id=? AND kind='reply' AND channel='im' AND state='pending'
                 AND (? >= ? OR EXISTS(
                     SELECT 1 FROM outbox AS prior WHERE prior.answer_uuid=candidate.answer_uuid
                     AND prior.channel='im'
                     AND prior.state IN ('sent','uncertain','rejected')))
               ORDER BY created_ns LIMIT 1''',
            (session_id, now, session['cooldown_until'])).fetchone()
        return dict(row) if row else None

    def pending_im_rows(self, *, limit=10):
        return [dict(row) for row in self.db.execute(
            "SELECT uuid,session_id,kind,content,channel,recipient_id,created_ns,"
            "0 AS attempts FROM outbox "
            "WHERE kind='reply' AND channel='im' AND state='pending' "
            'ORDER BY created_ns LIMIT ?', (limit,))]

    def mark_sending(self, message_uuid):
        with self.unit_of_work() as tx:
            tx.outbox.claim_sending(message_uuid)

    def mark_sent(self, message_uuid, *, now, interval):
        with self.unit_of_work() as tx:
            row = tx.outbox.confirm_sent(message_uuid, now=now)
            tx.sessions.set_cooldown(row['session_id'], now + interval)

    def mark_send_error(self, message_uuid, reason, *, uncertain):
        with self.unit_of_work() as tx:
            tx.outbox.mark_error(message_uuid, reason, uncertain=uncertain)

    def resolve_reply(self, session_id, message_uuid, outcome, *, now):
        if outcome not in ('sent', 'retry', 'cancel'):
            raise StorageError('invalid reply resolution')
        with self.unit_of_work() as tx:
            row = tx.outbox.resolve(session_id, message_uuid, outcome, now=now)
            if outcome == 'retry' and row['kind'] == 'reply':
                tx.sessions.schedule_reply_recheck(session_id)

    def discard_expired_replies(self, session_id, settings, *, now):
        session = self.require_session(session_id)
        cancelled = 0
        with self.unit_of_work() as tx:
            for row in tx.connection.execute(
                    "SELECT uuid,evidence FROM outbox WHERE session_id=? AND kind='reply' "
                    "AND state='pending' AND channel='im'", (session_id,)).fetchall():
                valid = True
                for key, fingerprint in decode_mapping(row['evidence']).items():
                    current = tx.connection.execute(
                        'SELECT body,suppressed FROM context WHERE session_id=? AND sentence_key=?',
                        (session_id, key)).fetchone()
                    if not current or current['suppressed']:
                        valid = False
                        break
                    caption = caption_from_json(current['body'])
                    if caption.fingerprint != fingerprint or not is_reply_evidence_caption(
                            caption, now=now, joined_at=session['started_at'], settings=settings):
                        valid = False
                        break
                if not valid:
                    tx.connection.execute(
                        "UPDATE outbox SET state='cancelled',error='reply_expired_or_revised' "
                        'WHERE uuid=?', (row['uuid'],))
                    cancelled += 1
        return cancelled

    def reply_statistics(self, session_id):
        counts = {r['state']: r['n'] for r in self.db.execute(
            'SELECT state,count(*) n FROM outbox WHERE session_id=? GROUP BY state', (session_id,))}
        return {'pending_replies': counts.get('pending', 0),
                'uncertain_replies': counts.get('uncertain', 0) + counts.get('sending', 0),
                'rejected_replies': counts.get('rejected', 0)}

    def drain_ready(self, session_id):
        """STATE_MACHINES S3 排空条件。"""
        session = self.require_session(session_id)
        if session['phase'] not in ('recording', 'draining'):
            return False
        if not self.sources_ready_for_drain(session_id):
            return False
        if self.inflight_jobs(session_id):
            return False
        pending = self.db.execute(
            "SELECT 1 FROM outbox WHERE session_id=? AND state='pending' LIMIT 1",
            (session_id,)).fetchone()
        return pending is None

    # -- subscriptions ------------------------------------------------------

    def create_subscription_request(self, session_id, subscriber_open_id, *, now=None):
        moment = time.time() if now is None else now
        with self.unit_of_work() as tx:
            session = tx.sessions.require(session_id)
            if session['phase'] != 'recording':
                raise StateError('not_found')
            latest = tx.subscriptions.latest_for(session_id, subscriber_open_id)
            if latest is not None:
                if latest['state'] == 'approved':
                    return {'request_no': latest['request_no'], 'state': 'approved',
                            'existing': True, 'session_id': session_id,
                            'owner_open_id': session['owner_open_id'], 'title': session['title']}
                if latest['state'] == 'revoked':
                    raise StateError('invalid_state')
            row = tx.subscriptions.insert(session_id, subscriber_open_id, now=moment)
            previous = ({'request_no': latest['request_no'], 'state': latest['state']}
                        if latest is not None else None)
        return {'request_no': row['request_no'], 'state': 'requested', 'existing': False,
                'session_id': session_id, 'owner_open_id': session['owner_open_id'],
                'title': session['title'], 'previous': previous}

    def decide_subscription(self, request_no, owner_open_id, decision, *, now=None):
        if decision not in ('approve', 'deny'):
            raise StorageError('invalid decision')
        moment = time.time() if now is None else now
        with self.unit_of_work() as tx:
            row = tx.subscriptions.require(request_no)
            session = tx.sessions.require(row['session_id'])
            if session['owner_open_id'] != owner_open_id:
                raise StateError('not_owner')
            if row['state'] == 'revoked':
                raise StateError('invalid_state')
            if row['state'] != 'requested':
                return {'request_no': request_no, 'state': row['state']}  # 幂等回显
            settled = tx.subscriptions.settle(
                request_no, 'approved' if decision == 'approve' else 'denied', now=moment)
        return {'request_no': request_no, 'state': settled['state']}

    def revoke_subscription(self, request_no, owner_open_id, *, now=None):
        moment = time.time() if now is None else now
        with self.unit_of_work() as tx:
            row = tx.subscriptions.require(request_no)
            session = tx.sessions.require(row['session_id'])
            if session['owner_open_id'] != owner_open_id:
                raise StateError('not_owner')
            if row['state'] != 'approved':
                raise StateError('invalid_state')
            settled = tx.subscriptions.settle(request_no, 'revoked', now=moment,
                                              expected='approved')
            keep = set(tx.subscriptions.approved_recipients(row['session_id'], owner_open_id))
            keep.discard(row['subscriber_open_id'])
            tx.outbox.cancel_pending_recipients(row['session_id'], keep)
        return {'request_no': request_no, 'state': settled['state']}

    def subscriptions_for(self, session_id):
        return [dict(row) for row in self.db.execute(
            'SELECT request_no,subscriber_open_id,state FROM subscriptions '
            'WHERE session_id=? ORDER BY request_no', (session_id,))]

    def status(self, session_id, *, now=None):
        session = self.require_session(session_id)
        now = time.time() if now is None else now
        pending = max(0, session['generation'] - session['decided_generation'])
        running = self.db.execute(
            "SELECT 1 FROM jobs WHERE session_id=? AND state='running' LIMIT 1",
            (session_id,)).fetchone()
        state = ('running' if running else 'backoff' if pending and session['retry_at'] > now
                 else 'waiting' if pending else 'idle')
        sources = [dict(row) for row in self.db.execute(
            'SELECT source,state,acked_sequence FROM sources WHERE session_id=?', (session_id,))]
        return {'session_id': session_id, 'phase': session['phase'],
                'owner_open_id': session['owner_open_id'],
                'meeting_no': session['meeting_no'], 'title': session['title'],
                'started_at': session['started_at'],
                'model_state': state, 'model_error': session['error'],
                'pending_model_inputs': pending,
                'material_state': session['material_state'],
                'material_present': session['material_state'] != MaterialState.DISABLED.value,
                'sources': sources,
                'subscriptions': self.subscriptions_for(session_id),
                } | self.reply_statistics(session_id)
