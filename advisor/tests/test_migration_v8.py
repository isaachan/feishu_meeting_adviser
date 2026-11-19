"""Schema v4 -> v11 migration tests (STATE_MACHINES v2 §6.2)."""
import unittest
from pathlib import Path
import sqlite3
import tempfile

from advisor.persistence import meeting_instance_key
from advisor.store import SessionStore

V4_SESSIONS = '''CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY, owner_open_id TEXT NOT NULL,
    meeting_no TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
    event_calendar_id TEXT NOT NULL, event_id TEXT NOT NULL,
    phase TEXT NOT NULL DEFAULT 'recording', stream_token TEXT NOT NULL,
    generation INTEGER NOT NULL DEFAULT 0, decided_generation INTEGER NOT NULL DEFAULT 0,
    error TEXT, retry_at REAL NOT NULL DEFAULT 0, cooldown_until REAL NOT NULL DEFAULT 0,
    material_version TEXT, material_state TEXT NOT NULL DEFAULT 'disabled',
    created_ns INTEGER NOT NULL, started_at REAL NOT NULL, ended_ns INTEGER,
    idle_timeout_s INTEGER NOT NULL DEFAULT 900,
    nsdp_hold INTEGER NOT NULL DEFAULT 0,
    lease_owner TEXT, lease_expires_at REAL)'''


def _make_v4_db(path: Path):
    db = sqlite3.connect(path)
    db.execute(V4_SESSIONS)
    db.execute('''CREATE TABLE inputs (
        delivery_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, sequence INTEGER NOT NULL,
        body TEXT NOT NULL, created_ns INTEGER NOT NULL)''')
    db.execute('''CREATE TABLE context (
        session_id TEXT NOT NULL, sentence_key TEXT NOT NULL, body TEXT NOT NULL,
        suppressed INTEGER NOT NULL DEFAULT 0, generation INTEGER NOT NULL,
        PRIMARY KEY (session_id,sentence_key))''')
    db.execute('''CREATE TABLE jobs (
        job_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, generation INTEGER NOT NULL,
        snapshot TEXT NOT NULL, role_version TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending', result TEXT, created_ns INTEGER NOT NULL,
        context_version TEXT)''')
    db.execute('''CREATE TABLE outbox (
        uuid TEXT PRIMARY KEY, session_id TEXT NOT NULL, kind TEXT NOT NULL,
        content TEXT NOT NULL, evidence TEXT NOT NULL, generation INTEGER NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending', error TEXT, created_ns INTEGER NOT NULL,
        sent_at REAL, context_version TEXT, answer_uuid TEXT NOT NULL,
        channel TEXT NOT NULL, recipient_id TEXT)''')
    db.execute('''CREATE TABLE session_materials (
        session_id TEXT PRIMARY KEY, body TEXT NOT NULL, loaded_ns INTEGER NOT NULL)''')
    db.execute('''CREATE TABLE sources (
        session_id TEXT NOT NULL, source TEXT NOT NULL,
        acked_sequence INTEGER NOT NULL DEFAULT 0, last_sequence INTEGER,
        last_seen_ns INTEGER NOT NULL DEFAULT 0, state TEXT NOT NULL DEFAULT 'live',
        lost_from INTEGER, lost_to INTEGER, speaker TEXT,
        PRIMARY KEY (session_id,source))''')
    db.execute('''CREATE TABLE subscriptions (
        request_no INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
        subscriber_open_id TEXT NOT NULL, state TEXT NOT NULL,
        created_ns INTEGER NOT NULL, decided_ns INTEGER)''')
    db.execute('''CREATE TABLE idempotency (
        key TEXT PRIMARY KEY, response TEXT NOT NULL, created_ns INTEGER NOT NULL)''')
    db.execute('''CREATE TABLE gui_devices (
        token_sha256 TEXT PRIMARY KEY, open_id TEXT NOT NULL,
        user_name TEXT, user_email TEXT,
        created_ns INTEGER NOT NULL, last_seen_ns INTEGER NOT NULL)''')
    db.execute('''CREATE TABLE users (
        open_id TEXT PRIMARY KEY, name TEXT, en_name TEXT, email TEXT,
        checked_ns INTEGER NOT NULL DEFAULT 0)''')
    db.execute("INSERT INTO sessions(session_id,owner_open_id,meeting_no,title,"
               "event_calendar_id,event_id,phase,stream_token,created_ns,started_at) "
               "VALUES ('s-live','ou_A','123456789','t','cal_x','evt_y',"
               "'recording','st-1',1000,10.0)")
    db.execute("INSERT INTO sessions(session_id,owner_open_id,meeting_no,title,"
               "event_calendar_id,event_id,phase,stream_token,created_ns,started_at,"
               "ended_ns) VALUES ('s-old','ou_Z','123456789','t2','cal_x','evt_z',"
               "'ended','st-2',900,9.0,950)")
    db.execute("INSERT INTO subscriptions(session_id,subscriber_open_id,state,"
               "created_ns,decided_ns) VALUES ('s-live','ou_B','approved',1001,1002)")
    db.execute("INSERT INTO subscriptions(session_id,subscriber_open_id,state,"
               "created_ns,decided_ns) VALUES ('s-live','ou_C','revoked',1001,1002)")
    db.execute('PRAGMA user_version=4')
    db.commit()
    db.close()
    path.chmod(0o600)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'adviser.sqlite3'
        _make_v4_db(self.path)

    def open(self):
        store = SessionStore(self.path)
        self.addCleanup(store.close)
        return store

    def test_v4_upgrades_to_v5_in_place(self):
        store = self.open()
        version = store.db.execute('PRAGMA user_version').fetchone()[0]
        self.assertEqual(version, 11)
        jobs_columns = {row['name']
                        for row in store.db.execute('PRAGMA table_info(jobs)')}
        self.assertIn('timings', jobs_columns)
        phases = {row['session_id']: row['phase']
                  for row in store.db.execute('SELECT session_id,phase FROM sessions')}
        self.assertEqual(phases, {'s-live': 'running', 's-old': 'ended'})
        live = store.require_session('s-live')
        self.assertEqual(live['organizer_open_id'], 'ou_A')
        self.assertEqual(live['last_audio_at'], 10.0)
        self.assertIsNone(live['drain_deadline_at'])  # running: no deadline yet
        self.assertTrue(live['meeting_instance_key'].startswith('legacy|123456789|'))

    def test_claims_backfilled(self):
        store = self.open()
        live_claims = {(row['user_open_id'], row['kind'], row['active'])
                       for row in store.db.execute(
                           "SELECT * FROM session_claims WHERE session_id='s-live'")}
        self.assertIn(('ou_A', 'controller', 1), live_claims)
        self.assertIn(('ou_B', 'subscriber', 1), live_claims)  # approved on non-ended
        self.assertNotIn(('ou_C', 'subscriber', 1), live_claims)  # revoked never claimed
        old = [row for row in store.db.execute(
            "SELECT * FROM session_claims WHERE session_id='s-old'")]
        self.assertTrue(all(row['active'] == 0 for row in old))

    def test_migrated_owner_can_claim_new_session_after_end(self):
        store = self.open()
        store.set_phase('s-live', 'draining')
        store.set_phase('s-live', 'ended')
        session, existing = store.create_session(
            owner_open_id='ou_A', meeting_no='555000111', title='新',
            event_calendar_id='cal_x', event_id='evt_new', now=100.0,
            instance_key=meeting_instance_key('555000111', 'cal_x', 'evt_new', 1762800000))
        self.assertFalse(existing)
        self.assertEqual(session['phase'], 'running')

    def test_fresh_database_still_initializes(self):
        fresh = Path(self.tmp.name) / 'fresh.sqlite3'
        SessionStore.initialize(fresh)
        store = SessionStore(fresh)
        self.addCleanup(store.close)
        self.assertEqual(store.db.execute('PRAGMA user_version').fetchone()[0], 11)

    def test_v9_adds_app_config_table(self):
        # fresh v10 db downgraded to v9 (drop app_config) then reopened
        v9 = Path(self.tmp.name) / 'v9.sqlite3'
        SessionStore.initialize(v9)
        with sqlite3.connect(v9) as db:
            db.execute('DROP TABLE app_config')
            db.execute('PRAGMA user_version=9')
        store = SessionStore(v9)
        self.addCleanup(store.close)
        self.assertEqual(store.db.execute('PRAGMA user_version').fetchone()[0], 11)
        self.assertIsNone(store.app_config_value('idle_timeout_s'))
        row = store.put_app_config('idle_timeout_s', 1200, now=2.0)
        self.assertEqual((row['key'], row['value'], row['updated_by']),
                         ('idle_timeout_s', '1200', 'admin'))
        self.assertEqual(store.app_config_value('idle_timeout_s'), 1200)

    def test_v8_adds_users_user_id_column(self):
        # fresh v9 db downgraded to a pre-v9 users shape (no user_id column)
        early = Path(self.tmp.name) / 'v8.sqlite3'
        SessionStore.initialize(early)
        with sqlite3.connect(early) as db:
            db.execute('ALTER TABLE users DROP COLUMN user_id')
            db.execute("INSERT INTO users(open_id,name,email,checked_ns) "
                       "VALUES ('ou_K','Kai HAN',NULL,1)")
            db.execute('PRAGMA user_version=8')
        store = SessionStore(early)
        self.addCleanup(store.close)
        cols = {row['name'] for row in store.db.execute('PRAGMA table_info(users)')}
        self.assertIn('user_id', cols)
        row = store.db.execute("SELECT * FROM users WHERE open_id='ou_K'").fetchone()
        self.assertEqual(row['name'], 'Kai HAN')
        self.assertIsNone(row['user_id'])

    def test_early_v5_without_observation_columns_is_upgraded(self):
        early = Path(self.tmp.name) / 'early.sqlite3'
        SessionStore.initialize(early)
        with sqlite3.connect(early) as db:
            db.execute('DROP INDEX sessions_vc_meeting')
            for col in ('owner_presence', 'observed_at', 'vc_meeting_id'):
                db.execute(f'ALTER TABLE sessions DROP COLUMN {col}')
            db.execute('PRAGMA user_version=5')
        store = SessionStore(early)
        self.addCleanup(store.close)
        cols = {row['name'] for row in store.db.execute('PRAGMA table_info(sessions)')}
        self.assertTrue({'owner_presence', 'observed_at', 'vc_meeting_id'} <= cols)
        self.assertEqual(store.db.execute('PRAGMA user_version').fetchone()[0], 11)

    def test_legacy_requested_rows_get_deadlines_and_single_claim(self):
        with sqlite3.connect(self.path) as db:
            for ns in (1200, 1300):
                db.execute(
                    "INSERT INTO subscriptions(session_id,subscriber_open_id,state,created_ns) "
                    "VALUES ('s-live','ou_C','requested',?)", (ns,))
        store = self.open()
        rows = store.db.execute(
            "SELECT request_no,state,approval_deadline_at FROM subscriptions "
            "WHERE subscriber_open_id='ou_C' ORDER BY request_no").fetchall()
        self.assertEqual([row['state'] for row in rows],
                         ['revoked', 'expired', 'requested'])
        self.assertEqual(rows[-1]['approval_deadline_at'], 1300 / 1e9 + 1800)
        self.assertEqual(store.active_claim('ou_C')['session_id'], 's-live')


if __name__ == '__main__':
    unittest.main()
