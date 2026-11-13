"""SessionStore: session lifecycle, ingest, jobs, outbox, subscriptions, leases.

Facade over persistence.py repositories; ports the legacy AdviserStore surface
used by the supervisor, with STATE_MACHINES.md semantics (no paused phase,
revoked terminal, draining/ended reject new subscriptions and late results).
"""
import functools
import hashlib
from pathlib import Path
import secrets
import threading
import time
from uuid import NAMESPACE_URL, uuid4, uuid5

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
            'sources', 'subscriptions', 'idempotency', 'gui_devices', 'users'}:
        raise StorageError('adviser database schema is unsupported; reinitialize')


def upgrade_schema(database: SQLiteDatabase) -> None:
    """In-place upgrades between SCHEMA_VERSIONs (idempotent per version).

    v3→v4: identity directory table ``users`` (CONTRACT_ADVISOR_API §9.1).
    """
    version = database.connection.execute('PRAGMA user_version').fetchone()[0]
    if version == SCHEMA_VERSION:
        return
    if version == 3:
        with database.transaction():
            database.connection.execute(
                '''CREATE TABLE IF NOT EXISTS users (
                    open_id TEXT PRIMARY KEY, name TEXT, en_name TEXT, email TEXT,
                    checked_ns INTEGER NOT NULL DEFAULT 0)''')
            # seed from gui_devices so existing GUI users keep their display names
            database.connection.execute(
                '''INSERT OR IGNORE INTO users(open_id,name,email,checked_ns)
                   SELECT open_id, MAX(user_name), MAX(user_email), 0
                   FROM gui_devices GROUP BY open_id''')
            database.connection.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
        return
    # fresh (0/empty) and unknown versions are judged by require_schema


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


def _locked(method):
    """Serialize store access across threads (ingest loop, supervisor, API)."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


class SessionStore:
    def __init__(self, path: Path, *, max_database_bytes=150_000_000,
                 max_payload_bytes=100_000_000):
        self.path = Path(path)
        self.max_payload_bytes = max_payload_bytes
        self._lock = threading.RLock()
        database = SQLiteDatabase.open_current(self.path, max_database_bytes=max_database_bytes)
        self.db = database.connection
        try:
            upgrade_schema(database)
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

    @_locked
    def payload_bytes(self):
        return sum(self.db.execute(
            f'SELECT COALESCE(SUM(length(CAST({column} AS BLOB))),0) FROM {table}').fetchone()[0]
            for table, column in (('inputs', 'body'), ('context', 'body'),
                                  ('jobs', 'snapshot'), ('jobs', 'result'),
                                  ('outbox', 'content'), ('outbox', 'evidence'),
                                  ('session_materials', 'body')))

    def unit_of_work(self, *, fault=None) -> UnitOfWork:
        return UnitOfWork(self.db, capacity_check=self._check_capacity, fault=fault,
                          lock=self._lock)

    # -- sessions ---------------------------------------------------------

    def create_session(self, *, owner_open_id, meeting_no, title, event_calendar_id,
                       event_id, description='', idle_timeout_s=900, now=None,
                       nsdp_text=''):
        moment = time.time() if now is None else now
        session_id = 's-' + uuid4().hex
        stream_token = 'st-' + secrets.token_urlsafe(32)
        description = description or ''
        documents = [{'kind': 'nsdp', 'text': nsdp_text}] if nsdp_text else []
        has_content = bool(description) or bool(documents)
        material = {'state': MaterialState.READY.value if has_content else MaterialState.DISABLED.value,
                    'context_version': self._material_version(description, documents)
                    if has_content else None,
                    'description': description, 'documents': documents}
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

    @_locked
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

    @_locked
    def session_token(self, session_id):
        row = self.db.execute(
            'SELECT stream_token,phase FROM sessions WHERE session_id=?', (session_id,)).fetchone()
        return dict(row) if row else None

    @_locked
    def list_sessions(self, owner_open_id=None):
        query = ("SELECT * FROM sessions WHERE phase IN ('recording','draining')"
                 " ORDER BY started_at DESC")
        params = ()
        if owner_open_id is not None:
            query = query.replace('WHERE phase', 'WHERE owner_open_id=? AND phase')
            params = (owner_open_id,)
        return [dict(row) for row in self.db.execute(query, params)]

    @_locked
    def list_sessions_all(self, limit=500):
        """Admin face (CONTRACT_ADVISOR_API §9): every owner, every phase."""
        rows = self.db.execute(
            'SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?', (limit,))
        return [dict(row) for row in rows]

    # -- identity directory (CONTRACT_ADVISOR_API §9.1) ----------------------

    @_locked
    def upsert_user(self, open_id, *, name=None, en_name=None, email=None, now=None):
        """Latest-wins profile upsert; NULL fields keep their existing values."""
        moment = time.time() if now is None else now
        self.db.execute(
            '''INSERT INTO users(open_id,name,en_name,email,checked_ns) VALUES(?,?,?,?,?)
               ON CONFLICT(open_id) DO UPDATE SET
                 name=COALESCE(excluded.name,users.name),
                 en_name=COALESCE(excluded.en_name,users.en_name),
                 email=COALESCE(excluded.email,users.email),
                 checked_ns=excluded.checked_ns''',
            (open_id, name, en_name, email, int(moment * 1_000_000_000)))

    @_locked
    def users_map(self, open_ids):
        ids = tuple(open_ids)
        if not ids:
            return {}
        rows = self.db.execute(
            f"SELECT * FROM users WHERE open_id IN ({','.join('?' * len(ids))})", ids)
        return {row['open_id']: dict(row) for row in rows}

    @_locked
    def all_subscriptions(self, limit=2000):
        """Admin face: every subscription row, newest request_no first."""
        rows = self.db.execute(
            'SELECT request_no,session_id,subscriber_open_id,state FROM subscriptions '
            'ORDER BY request_no DESC LIMIT ?', (limit,))
        return [dict(row) for row in rows]

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

    @_locked
    def active_by_meeting_no(self, meeting_no):
        """S3 注册表：正被服务中（recording）的会话。"""
        row = self.db.execute(
            "SELECT * FROM sessions WHERE meeting_no=? AND phase='recording' "
            'ORDER BY started_at DESC LIMIT 1', (meeting_no,)).fetchone()
        return dict(row) if row else None

    # -- gui devices (CONTRACT_ADVISOR_API §8) --------------------------------

    @staticmethod
    def _device_sha256(token):
        return hashlib.sha256(token.encode('utf-8')).hexdigest()

    @_locked
    def register_device(self, token, open_id, *, name=None, email=None, now=None):
        """Store a device credential by hash only (plaintext never persisted)."""
        moment = time.time() if now is None else now
        ns = int(moment * 1_000_000_000)
        with self.unit_of_work() as tx:
            tx.connection.execute(
                'INSERT OR REPLACE INTO gui_devices(token_sha256,open_id,user_name,'
                'user_email,created_ns,last_seen_ns) VALUES (?,?,?,?,?,?)',
                (self._device_sha256(token), open_id, name, email, ns, ns))

    @_locked
    def device(self, token, *, now=None):
        """Resolve a device token to its row (or None); refreshes last_seen."""
        if not isinstance(token, str) or not token:
            return None
        row = self.db.execute('SELECT * FROM gui_devices WHERE token_sha256=?',
                              (self._device_sha256(token),)).fetchone()
        if row is None:
            return None
        moment = time.time() if now is None else now
        self.db.execute('UPDATE gui_devices SET last_seen_ns=? WHERE token_sha256=?',
                        (int(moment * 1_000_000_000), self._device_sha256(token)))
        return dict(row)

    @_locked
    def drop_device(self, token):
        if not isinstance(token, str) or not token:
            return
        with self.unit_of_work() as tx:
            tx.connection.execute('DELETE FROM gui_devices WHERE token_sha256=?',
                                  (self._device_sha256(token),))

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

    def clear_leases(self):
        """Startup-only: single-host v1 assumes prior holders are gone."""
        with self.unit_of_work() as tx:
            tx.connection.execute(
                'UPDATE sessions SET lease_owner=NULL, lease_expires_at=NULL '
                'WHERE lease_owner IS NOT NULL')

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

    @_locked
    def live_sources(self, session_id):
        return [row['source'] for row in self.db.execute(
            "SELECT source FROM sources WHERE session_id=? AND state='live'",
            (session_id,))]

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

    def source_reset(self, session_id, source, acked_sequence):
        """New capture epoch (resume:false after stale): reset cursor, keep live."""
        with self.unit_of_work() as tx:
            tx.connection.execute(
                "UPDATE sources SET state='live',acked_sequence=?,lost_from=NULL,"
                'lost_to=NULL WHERE session_id=? AND source=?',
                (acked_sequence, session_id, source))

    def record_source_gap(self, session_id, source, lost_from, lost_to):
        """Explicit missing range (resume with first_sequence > cursor+1)."""
        with self.unit_of_work() as tx:
            tx.connection.execute(
                "UPDATE sources SET lost_from=?,lost_to=? "
                'WHERE session_id=? AND source=?', (lost_from, lost_to, session_id, source))

    @_locked
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

    @_locked
    def context_rows(self, session_id, *, limit):
        return [dict(row) for row in self.db.execute(
            'SELECT * FROM context WHERE session_id=? ORDER BY generation DESC LIMIT ?',
            (session_id, limit))]

    @_locked
    def context_count(self, session_id):
        return self.db.execute(
            'SELECT count(*) FROM context WHERE session_id=?', (session_id,)).fetchone()[0]

    @_locked
    def context_row(self, session_id, sentence_key):
        row = self.db.execute(
            'SELECT body,suppressed FROM context WHERE session_id=? AND sentence_key=?',
            (session_id, sentence_key)).fetchone()
        return dict(row) if row else None

    @_locked
    def materials(self, session_id):
        row = self.db.execute(
            'SELECT body FROM session_materials WHERE session_id=?', (session_id,)).fetchone()
        return decode_mapping(row['body']) if row else None

    # -- NSDP material / hold / owner notices ----------------------------------

    @staticmethod
    def _material_version(description, documents) -> str:
        return digest(['material-v1', description,
                       *(item.get('text', '') for item in documents)])

    @_locked
    def update_nsdp_material(self, session_id, text, *, now=None):
        """Replace the nsdp document in the session material (refresh/prefetch)."""
        moment = time.time() if now is None else now
        with self.unit_of_work() as tx:
            row = tx.connection.execute(
                'SELECT body FROM session_materials WHERE session_id=?',
                (session_id,)).fetchone()
            material = decode_mapping(row['body']) if row else {
                'state': MaterialState.DISABLED.value, 'context_version': None,
                'description': '', 'documents': []}
            documents = [item for item in material.get('documents', [])
                         if item.get('kind') != 'nsdp']
            if text:
                documents.append({'kind': 'nsdp', 'text': text})
            material['documents'] = documents
            description = material.get('description') or ''
            has_content = bool(description) or bool(documents)
            material['state'] = (MaterialState.READY.value if has_content
                                 else MaterialState.DISABLED.value)
            material['context_version'] = (self._material_version(description, documents)
                                           if has_content else None)
            tx.connection.execute(
                'UPDATE session_materials SET body=?, loaded_ns=? WHERE session_id=?',
                (encode(material), int(moment * 1_000_000_000), session_id))
            tx.connection.execute(
                'UPDATE sessions SET material_version=?, material_state=? WHERE session_id=?',
                (material['context_version'], material['state'], session_id))

    @_locked
    def set_nsdp_hold(self, session_id, held, *, now=None):
        """Hold suspends all model decisions for the session (hard gate)."""
        with self.unit_of_work() as tx:
            tx.connection.execute(
                'UPDATE sessions SET nsdp_hold=? WHERE session_id=?',
                (1 if held else 0, session_id))

    @_locked
    def insert_owner_notice(self, session_id, recipient_open_id, content, *,
                            now=None, dedupe_s=300.0):
        """Queue one IM notice row for the organizer (nsdp re-auth nudges).

        The uuid is bucketed by dedupe window so retries never duplicate a nudge.
        """
        moment = time.time() if now is None else now
        notice_uuid = str(uuid5(NAMESPACE_URL,
                                f'nsdp-notice:{session_id}:{int(moment // max(1.0, dedupe_s))}'))
        with self.unit_of_work() as tx:
            tx.connection.execute(
                "INSERT OR IGNORE INTO outbox(uuid,session_id,kind,content,evidence,"
                "generation,created_ns,context_version,answer_uuid,channel,recipient_id,state) "
                "VALUES (?,?,'reply',?,'{}',0,?,NULL,?,'im',?,'pending')",
                (notice_uuid, session_id, content, int(moment * 1_000_000_000),
                 notice_uuid, recipient_open_id))
        return notice_uuid

    # -- snapshot / jobs / decisions ----------------------------------------

    def snapshot(self, session_id, settings, *, now):
        session = self.require_session(session_id)
        return SnapshotBuilder(self).build(session_id, settings, now=now,
                                           joined_at=session['started_at'])

    @_locked
    def pending_reply_row(self, session_id):
        row = self.db.execute(
            "SELECT * FROM outbox WHERE session_id=? AND kind='reply' AND state='pending' "
            "AND channel='im' ORDER BY created_ns DESC LIMIT 1", (session_id,)).fetchone()
        return dict(row) if row else None

    @_locked
    def recent_reply_rows(self, session_id, *, limit):
        return [dict(row) for row in self.db.execute(
            "SELECT content,state FROM outbox WHERE session_id=? "
            "AND state IN ('sent','uncertain') GROUP BY answer_uuid "
            'ORDER BY created_ns DESC LIMIT ?', (session_id, limit))]

    @_locked
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

    @_locked
    def inflight_jobs(self, session_id):
        return self.db.execute(
            "SELECT count(*) FROM jobs WHERE session_id=? AND state IN ('pending','running')",
            (session_id,)).fetchone()[0]

    # -- outbox transport surface -------------------------------------------

    @_locked
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

    @_locked
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

    @_locked
    def reply_statistics(self, session_id):
        counts = {r['state']: r['n'] for r in self.db.execute(
            'SELECT state,count(*) n FROM outbox WHERE session_id=? GROUP BY state', (session_id,))}
        return {'pending_replies': counts.get('pending', 0),
                'uncertain_replies': counts.get('uncertain', 0) + counts.get('sending', 0),
                'rejected_replies': counts.get('rejected', 0)}

    @_locked
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

    def revoke_subscription(self, request_no, caller_open_id, *, now=None):
        """Approved -> revoked by the session owner OR the subscriber themself."""
        moment = time.time() if now is None else now
        with self.unit_of_work() as tx:
            row = tx.subscriptions.require(request_no)
            session = tx.sessions.require(row['session_id'])
            caller_is_owner = session['owner_open_id'] == caller_open_id
            caller_is_subscriber = row['subscriber_open_id'] == caller_open_id
            if not (caller_is_owner or caller_is_subscriber):
                raise StateError('not_owner')
            if row['state'] != 'approved':
                raise StateError('invalid_state')
            settled = tx.subscriptions.settle(request_no, 'revoked', now=moment,
                                              expected='approved')
            keep = set(tx.subscriptions.approved_recipients(row['session_id'],
                                                            session['owner_open_id']))
            keep.discard(row['subscriber_open_id'])
            tx.outbox.cancel_pending_recipients(row['session_id'], keep)
        return {'request_no': request_no, 'state': settled['state']}

    @_locked
    def subscriptions_for_subscriber(self, subscriber_open_id):
        rows = self.db.execute(
            'SELECT s.request_no, s.session_id, s.state, e.meeting_no, e.title '
            'FROM subscriptions s JOIN sessions e ON e.session_id = s.session_id '
            'WHERE s.subscriber_open_id=? ORDER BY s.request_no DESC',
            (subscriber_open_id,)).fetchall()
        return [{'request_no': row['request_no'], 'session_id': row['session_id'],
                 'meeting_no': row['meeting_no'], 'title': row['title'],
                 'state': row['state']} for row in rows]

    def subscriptions_for(self, session_id):
        return [dict(row) for row in self.db.execute(
            'SELECT request_no,subscriber_open_id,state FROM subscriptions '
            'WHERE session_id=? ORDER BY request_no', (session_id,))]

    @_locked
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
                'nsdp_hold': bool(session['nsdp_hold']),
                'sources': sources,
                'subscriptions': self.subscriptions_for(session_id),
                } | self.reply_statistics(session_id)
