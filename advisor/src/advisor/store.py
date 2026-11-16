"""SessionStore: session lifecycle, ingest, jobs, outbox, subscriptions, leases.

Facade over persistence.py repositories; ports the legacy AdviserStore surface
used by the supervisor, with STATE_MACHINES.md semantics (no paused phase,
revoked terminal, draining/ended reject new subscriptions and late results).
"""
import functools
import hashlib
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from uuid import NAMESPACE_URL, uuid4, uuid5

from .errors import StateError, StorageError
from .models import (Caption, JobState, MaterialState, ReplyState, SourceState,
                     caption_from_json, caption_to_dict, digest)
from .persistence import (DEFAULT_DRAIN_DEADLINE_S, SCHEMA, SCHEMA_VERSION,
                          SESSION_PHASES, SESSION_TRANSITIONS,
                          UnitOfWork, decode_mapping, encode, meeting_instance_key)
from .snapshot import SnapshotBuilder, is_reply_evidence_caption
from .decision import DecisionApplier
from .sqlite import SQLiteDatabase

SESSION_TABLES = {
    'sessions', 'inputs', 'context', 'jobs', 'outbox', 'session_materials',
    'sources', 'subscriptions', 'idempotency', 'gui_devices', 'users',
    'session_claims', 'supervisor_grants'}


def _tables(db) -> set[str]:
    return {row['name'] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def require_schema(connection) -> None:
    version = connection.execute('PRAGMA user_version').fetchone()[0]
    tables = _tables(connection) - {'sqlite_sequence'}  # appears lazily with AUTOINCREMENT
    if version != SCHEMA_VERSION or tables != SESSION_TABLES:
        raise StorageError('adviser database schema is unsupported; reinitialize')


def upgrade_schema(database: SQLiteDatabase) -> None:
    """In-place upgrades between SCHEMA_VERSIONs (idempotent per version).

    v3→v4: identity directory table ``users`` (CONTRACT_ADVISOR_API §9.1).
    v4→v5: state machine v2 (STATE_MACHINES §6) — phase rename + paused,
    meeting-instance uniqueness, session claims, supervisor grants,
    subscription approval deadline/closed_by.
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
            database.connection.execute('PRAGMA user_version=4')
        version = 4
    if version == 4:
        with database.transaction():
            db = database.connection
            db.execute('''CREATE TABLE IF NOT EXISTS session_claims (
                user_open_id TEXT NOT NULL, session_id TEXT NOT NULL,
                kind TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                created_ns INTEGER NOT NULL, released_ns INTEGER,
                PRIMARY KEY (user_open_id, session_id))''')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS session_claims_active '
                       'ON session_claims(user_open_id) WHERE active=1')
            db.execute('''CREATE TABLE IF NOT EXISTS supervisor_grants (
                login_name TEXT PRIMARY KEY, open_id TEXT, enabled INTEGER NOT NULL DEFAULT 0,
                updated_ns INTEGER NOT NULL, updated_by TEXT NOT NULL DEFAULT 'admin')''')
            db.execute('ALTER TABLE subscriptions ADD COLUMN approval_deadline_at REAL')
            db.execute('ALTER TABLE subscriptions ADD COLUMN closed_by TEXT')
            db.execute('ALTER TABLE sessions ADD COLUMN meeting_instance_key TEXT')
            db.execute("ALTER TABLE sessions ADD COLUMN organizer_open_id "
                       "TEXT NOT NULL DEFAULT ''")
            db.execute('ALTER TABLE sessions ADD COLUMN last_audio_at REAL')
            db.execute('ALTER TABLE sessions ADD COLUMN drain_deadline_at REAL')
            db.execute('ALTER TABLE sessions ADD COLUMN owner_presence TEXT')
            db.execute('ALTER TABLE sessions ADD COLUMN observed_at REAL')
            db.execute("UPDATE sessions SET phase='running' WHERE phase='recording'")
            db.execute("UPDATE sessions SET organizer_open_id=owner_open_id "
                       "WHERE organizer_open_id=''")
            db.execute('UPDATE sessions SET last_audio_at=started_at '
                       'WHERE last_audio_at IS NULL')
            db.execute("UPDATE sessions SET meeting_instance_key="
                       "'legacy|'||meeting_no||'|'||event_calendar_id||'|'||event_id")
            # guarantee uniqueness of backfilled keys (duplicate historical
            # bindings get a rowid suffix; documented in STATE_MACHINES §6.2)
            duplicates = db.execute(
                'SELECT rowid, meeting_instance_key FROM sessions WHERE '
                'meeting_instance_key IN (SELECT meeting_instance_key FROM sessions '
                'GROUP BY meeting_instance_key HAVING count(*)>1)').fetchall()
            for row in duplicates:
                db.execute('UPDATE sessions SET meeting_instance_key=? '
                           "WHERE meeting_instance_key=? AND rowid!=?",
                           (f"{row['meeting_instance_key']}#{row['rowid']}",
                            row['meeting_instance_key'], row['rowid']))
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS sessions_instance '
                       'ON sessions(meeting_instance_key)')
            # controller claims: active only for non-ended legacy sessions
            db.execute('''INSERT OR IGNORE INTO session_claims(
                    user_open_id,session_id,kind,active,created_ns)
                SELECT owner_open_id, session_id, 'controller',
                       CASE WHEN phase='ended' THEN 0 ELSE 1 END, created_ns
                FROM sessions''')
            # subscriber claims: approved subscriptions on non-ended sessions
            db.execute('''INSERT OR IGNORE INTO session_claims(
                    user_open_id,session_id,kind,active,created_ns)
                SELECT s.subscriber_open_id, s.session_id, 'subscriber', 1, s.created_ns
                FROM subscriptions s JOIN sessions e ON e.session_id=s.session_id
                WHERE s.state='approved' AND e.phase!='ended' ''')
            db.execute('PRAGMA user_version=5')
        version = 5
    if version == 5:
        with database.transaction():
            db = database.connection
            # Early v5 databases may predate SM-06's observation columns.
            columns = {row['name'] for row in db.execute('PRAGMA table_info(sessions)')}
            for name, kind in (('owner_presence', 'TEXT'), ('observed_at', 'REAL'),
                               ('vc_meeting_id', 'TEXT')):
                if name not in columns:
                    db.execute(f'ALTER TABLE sessions ADD COLUMN {name} {kind}')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS sessions_vc_meeting '
                       'ON sessions(vc_meeting_id) WHERE vc_meeting_id IS NOT NULL')
            db.execute('PRAGMA user_version=6')
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
                       nsdp_text='', instance_key=None,
                       organizer_open_id=None):
        """STATE_MACHINES v2 S1: instance-unique creation with claim checks.

        Returns ``(session, existing)``. Raises StateError('session_claimed' |
        'instance_ended' | 'claim_conflict') per the frozen precedence:
        event-binding match among non-ended sessions first (reschedules keep
        one instance), then the exact instance key, then the single-claim rule.
        """
        moment = time.time() if now is None else now
        organizer = organizer_open_id or owner_open_id
        key = instance_key or meeting_instance_key(
            meeting_no, event_calendar_id, event_id, 0)
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
            binding_match = tx.connection.execute(
                "SELECT * FROM sessions WHERE meeting_no=? AND event_calendar_id=? "
                "AND event_id=? AND phase IN ('running','paused','draining') "
                'ORDER BY started_at DESC LIMIT 1',
                (meeting_no, event_calendar_id, event_id)).fetchone()
            if binding_match is not None:
                if binding_match['owner_open_id'] != owner_open_id:
                    raise StateError('session_claimed')
                value = dict(binding_match)
                value.pop('stream_token', None)
                return value, True
            instance_match = tx.connection.execute(
                'SELECT * FROM sessions WHERE meeting_instance_key=?',
                (key,)).fetchone()
            if instance_match is not None:
                if instance_match['phase'] in ('draining', 'ended'):
                    raise StateError('instance_ended')
                if instance_match['owner_open_id'] != owner_open_id:
                    raise StateError('session_claimed')
                value = dict(instance_match)
                value.pop('stream_token', None)
                return value, True
            active_claim = tx.connection.execute(
                'SELECT session_id FROM session_claims WHERE user_open_id=? AND active=1',
                (owner_open_id,)).fetchone()
            if active_claim is not None:
                raise StateError('claim_conflict')
            tx.connection.execute(
                '''INSERT INTO sessions(
                       session_id,owner_open_id,meeting_no,title,
                       event_calendar_id,event_id,meeting_instance_key,phase,
                       stream_token,organizer_open_id,material_version,
                       material_state,created_ns,started_at,last_audio_at,
                       idle_timeout_s)
                   VALUES (?,?,?,?,?,?,?,'running',?,?,?,?,?,?,?,?)''',
                (session_id, owner_open_id, meeting_no, title[:200], event_calendar_id,
                 event_id, key, stream_token, organizer,
                 material['context_version'], material['state'],
                 int(moment * 1_000_000_000), moment, moment, idle_timeout_s))
            tx.connection.execute(
                "INSERT INTO session_claims(user_open_id,session_id,kind,active,created_ns) "
                "VALUES (?,?,'controller',1,?)",
                (owner_open_id, session_id, int(moment * 1_000_000_000)))
            tx.connection.execute(
                'INSERT INTO session_materials(session_id,body,loaded_ns) VALUES (?,?,?)',
                (session_id, encode(material), int(moment * 1_000_000_000)))
        return self.session(session_id), False

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
        query = ("SELECT * FROM sessions WHERE phase IN ('running','paused','draining')"
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

    def set_phase(self, session_id, phase, *, now=None, drain_deadline_at=...):
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
            deadline = None
            if phase == 'draining':
                if drain_deadline_at is ...:
                    deadline = moment + DEFAULT_DRAIN_DEADLINE_S
                else:
                    deadline = drain_deadline_at
            tx.connection.execute(
                'UPDATE sessions SET phase=?,ended_ns=?,drain_deadline_at=? WHERE session_id=?',
                (phase, int(moment * 1_000_000_000) if phase == 'ended' else None,
                 deadline, session_id))
            if phase == 'ended':
                tx.connection.execute(
                    'UPDATE session_claims SET active=0,released_ns=? '
                    'WHERE session_id=? AND active=1',
                    (int(moment * 1_000_000_000), session_id))
        return self.session(session_id)

    # -- session observation input (STATE_MACHINES v2 §4; stub until SM-08) --

    def apply_observation(self, session_id, *, owner_presence=None, meeting_status=None,
                          observed_at=None, now=None, max_age_s=60.0):
        """Apply one presence/meeting snapshot; the only paused/ended driver.

        Rules (frozen): S2 needs the confirmed present→left turn (creation
        time absence/unknown never pauses); S3 needs present while paused;
        meeting ended drains; unknown/stale snapshots never migrate.
        Pausing discards in-flight work immediately (jobs abandoned, unsent
        pending IM cancelled) in the same transaction.
        """
        moment = time.time() if now is None else now
        snap_at = observed_at if observed_at is not None else moment
        ns = int(moment * 1_000_000_000)
        with self.unit_of_work() as tx:
            session = tx.sessions.require(session_id)
            phase = session['phase']
            if phase == 'ended':
                return {'session_id': session_id, 'phase': 'ended', 'applied': False,
                        'reason': 'ended'}
            if session['observed_at'] is not None and snap_at <= session['observed_at']:
                return {'session_id': session_id, 'phase': phase, 'applied': False,
                        'reason': 'stale'}
            if moment - snap_at > max_age_s:
                return {'session_id': session_id, 'phase': phase, 'applied': False,
                        'reason': 'stale'}
            prev = session['owner_presence']
            confirmed = owner_presence if owner_presence in ('present', 'left') else prev
            target = None
            if meeting_status == 'ended' and phase in ('running', 'paused'):
                target = 'draining'
            elif phase == 'running' and owner_presence == 'left' and prev == 'present':
                target = 'paused'
            elif phase == 'paused' and owner_presence == 'present':
                target = 'running'
            if target is None:
                tx.connection.execute(
                    'UPDATE sessions SET owner_presence=?,observed_at=? WHERE session_id=?',
                    (confirmed, snap_at, session_id))
                return {'session_id': session_id, 'phase': phase, 'applied': True}
            assert (phase, target) in SESSION_TRANSITIONS
            deadline = moment + DEFAULT_DRAIN_DEADLINE_S if target == 'draining' else None
            tx.connection.execute(
                'UPDATE sessions SET phase=?,owner_presence=?,observed_at=?,'
                'drain_deadline_at=CASE WHEN ? IS NULL THEN drain_deadline_at ELSE ? END '
                'WHERE session_id=?',
                (target, confirmed, snap_at, deadline, deadline, session_id))
            if target == 'paused':
                # 已定「立即作废」：在途任务作废、未发送回答取消（同事务）
                tx.connection.execute(
                    "UPDATE jobs SET state='abandoned' WHERE session_id=? "
                    "AND state IN ('pending','running')", (session_id,))
                tx.outbox.cancel_pending(session_id)
            if target == 'ended':
                tx.connection.execute(
                    'UPDATE session_claims SET active=0,released_ns=? '
                    'WHERE session_id=? AND active=1', (ns, session_id))
        return {'session_id': session_id, 'phase': target, 'applied': True}

    def bind_vc_meeting(self, session_id, meeting_id):
        """Bind a VC ID only when exactly one active occurrence owns the number."""
        with self.unit_of_work() as tx:
            session = tx.sessions.require(session_id)
            if session['phase'] not in ('running', 'paused'):
                return False
            if session['vc_meeting_id'] is not None:
                return session['vc_meeting_id'] == meeting_id
            others = tx.connection.execute(
                "SELECT 1 FROM sessions WHERE meeting_no=? AND session_id!=? "
                "AND phase IN ('running','paused') LIMIT 1",
                (session['meeting_no'], session_id)).fetchone()
            if others:
                return False
            try:
                tx.connection.execute(
                    'UPDATE sessions SET vc_meeting_id=? WHERE session_id=? '
                    'AND vc_meeting_id IS NULL', (meeting_id, session_id))
            except sqlite3.IntegrityError:
                return False
        return True

    @_locked
    def active_sessions_by_meeting_no(self, meeting_no):
        """S3 目标解析：同号全部 running/paused 会话（>1 个时上层报 meeting_ambiguous）。"""
        rows = self.db.execute(
            "SELECT * FROM sessions WHERE meeting_no=? AND phase IN ('running','paused') "
            'ORDER BY started_at DESC', (meeting_no,)).fetchall()
        return [dict(row) for row in rows]

    # -- session claims (STATE_MACHINES v2 §0) ------------------------------

    @_locked
    def active_claim(self, user_open_id):
        row = self.db.execute(
            'SELECT * FROM session_claims WHERE user_open_id=? AND active=1',
            (user_open_id,)).fetchone()
        return dict(row) if row else None

    @_locked
    def claims_for_session(self, session_id):
        return [dict(row) for row in self.db.execute(
            'SELECT * FROM session_claims WHERE session_id=? ORDER BY created_ns',
            (session_id,))]

    def release_claim(self, user_open_id, session_id, *, now=None):
        """Free one claim row (subscription closure path); idempotent."""
        moment = time.time() if now is None else now
        with self.unit_of_work() as tx:
            tx.connection.execute(
                'UPDATE session_claims SET active=0,released_ns=? '
                'WHERE user_open_id=? AND session_id=? AND active=1',
                (int(moment * 1_000_000_000), user_open_id, session_id))

    # -- supervisor grants (CONTRACT_ADVISOR_API §9.3/§9.4) ------------------

    @_locked
    def supervisor_grants(self):
        return [dict(row) for row in self.db.execute(
            'SELECT * FROM supervisor_grants ORDER BY login_name')]

    @_locked
    def enabled_supervisor_open_ids(self):
        return {row['open_id'] for row in self.db.execute(
            'SELECT open_id FROM supervisor_grants WHERE enabled=1 AND open_id IS NOT NULL')}

    def put_supervisor_grant(self, login_name, *, open_id, enabled, updated_by='admin',
                             now=None):
        if not isinstance(login_name, str) or not login_name:
            raise StorageError('invalid login_name')
        moment = time.time() if now is None else now
        with self.unit_of_work() as tx:
            tx.connection.execute(
                '''INSERT INTO supervisor_grants(login_name,open_id,enabled,updated_ns,updated_by)
                   VALUES (?,?,?,?,?)
                   ON CONFLICT(login_name) DO UPDATE SET
                     open_id=excluded.open_id, enabled=excluded.enabled,
                     updated_ns=excluded.updated_ns, updated_by=excluded.updated_by''',
                (login_name, open_id, 1 if enabled else 0,
                 int(moment * 1_000_000_000), updated_by))
        row = self.db.execute('SELECT * FROM supervisor_grants WHERE login_name=?',
                              (login_name,)).fetchone()
        return dict(row)

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
        """Advance the durable cursor; control messages do not refresh audio idle."""
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

    def record_audio_at(self, session_id, *, now=None):
        """Only an accepted binary audio frame may advance session idle time."""
        moment = time.time() if now is None else now
        with self.unit_of_work() as tx:
            session = tx.sessions.require(session_id)
            if session['phase'] != 'running':
                raise StateError('session_not_recording')
            tx.connection.execute(
                'UPDATE sessions SET last_audio_at=MAX(COALESCE(last_audio_at,0),?) '
                'WHERE session_id=?', (moment, session_id))

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
        """New capture epoch: reset cursor and prior run's drain marker."""
        with self.unit_of_work() as tx:
            tx.connection.execute(
                "UPDATE sources SET state='live',acked_sequence=?,last_sequence=NULL,lost_from=NULL,"
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
            if session['phase'] != 'running':
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
                            now=None, dedupe_s=300.0, provider='nsdp'):
        """Queue one IM notice row for the organizer (nsdp re-auth nudges).

        The uuid is bucketed by dedupe window so retries never duplicate a nudge.
        """
        moment = time.time() if now is None else now
        notice_uuid = str(uuid5(NAMESPACE_URL,
                                f'{provider}-notice:{session_id}:{int(moment // max(1.0, dedupe_s))}'))
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
            if current['phase'] not in ('running', 'draining'):
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
        if session['phase'] not in ('running', 'draining'):
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
        """STATE_MACHINES v2 T1: requesting occupies the caller's single claim."""
        moment = time.time() if now is None else now
        ns = int(moment * 1_000_000_000)
        with self.unit_of_work() as tx:
            session = tx.sessions.require(session_id)
            if session['phase'] not in ('running', 'paused'):
                raise StateError('not_found')
            if session['owner_open_id'] == subscriber_open_id:
                raise StateError('claim_conflict')  # controller cannot subscribe
            claim = tx.connection.execute(
                'SELECT session_id,kind FROM session_claims WHERE user_open_id=? AND active=1',
                (subscriber_open_id,)).fetchone()
            if claim is not None and claim['session_id'] != session_id:
                raise StateError('claim_conflict')
            latest = tx.subscriptions.latest_for(session_id, subscriber_open_id)
            if latest is not None:
                if latest['state'] == 'approved':
                    return {'request_no': latest['request_no'], 'state': 'approved',
                            'existing': True, 'session_id': session_id,
                            'owner_open_id': session['owner_open_id'], 'title': session['title']}
                if latest['state'] == 'revoked':
                    raise StateError('invalid_state')
            row = tx.subscriptions.insert(session_id, subscriber_open_id, now=moment)
            tx.connection.execute(
                '''INSERT INTO session_claims(user_open_id,session_id,kind,active,created_ns)
                   VALUES (?,?,'subscriber',1,?)
                   ON CONFLICT(user_open_id,session_id) DO UPDATE SET
                     active=1, released_ns=NULL''',
                (subscriber_open_id, session_id, ns))
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
            if settled['state'] == 'denied':
                tx.connection.execute(
                    'UPDATE session_claims SET active=0,released_ns=? '
                    'WHERE user_open_id=? AND session_id=? AND active=1',
                    (int(moment * 1_000_000_000), row['subscriber_open_id'],
                     row['session_id']))
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
            tx.connection.execute(
                'UPDATE session_claims SET active=0,released_ns=? '
                'WHERE user_open_id=? AND session_id=? AND active=1',
                (int(moment * 1_000_000_000), row['subscriber_open_id'],
                 row['session_id']))
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
