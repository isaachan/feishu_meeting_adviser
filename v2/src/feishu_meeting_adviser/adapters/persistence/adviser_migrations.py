"""Versioned adviser schema migrated from verified historical shapes."""
import sqlite3

from .sqlite import Migration, MigrationRunner, SQLiteDatabase, StorageError


ADVISER_SCHEMA_VERSION = 2
CORE_TABLES = {'meetings', 'inputs', 'context', 'jobs'}
KNOWN_TABLES = CORE_TABLES | {'outbox', 'meeting_materials'}
BASE_COLUMNS = {
    'meetings': {'meeting_id','generation','decided_generation','participation','source_state','error','retry_at','cooldown_until'},
    'inputs': {'delivery_id','meeting_id','sequence','body','created_ns'},
    'context': {'meeting_id','sentence_key','body','suppressed','generation'},
    'jobs': {'job_id','meeting_id','generation','snapshot','role_version','state','result','created_ns'},
}
V1_COLUMNS = BASE_COLUMNS | {
    'meetings': BASE_COLUMNS['meetings'] | {'join_instance','role_version','source_status'},
    'outbox': {'uuid','meeting_id','kind','content','evidence','generation','state','error','created_ns','sent_at'},
}
V2_COLUMNS = V1_COLUMNS | {
    'meetings': V1_COLUMNS['meetings'] | {'material_version','material_state'},
    'jobs': V1_COLUMNS['jobs'] | {'context_version'},
    'outbox': V1_COLUMNS['outbox'] | {'context_version'},
    'meeting_materials': {'meeting_id','body','loaded_ns'},
}

SCHEMA_V1 = '''
CREATE TABLE IF NOT EXISTS meetings (
    meeting_id TEXT PRIMARY KEY, generation INTEGER NOT NULL DEFAULT 0,
    decided_generation INTEGER NOT NULL DEFAULT 0, participation TEXT NOT NULL DEFAULT 'active',
    source_state TEXT, error TEXT, retry_at REAL NOT NULL DEFAULT 0,
    cooldown_until REAL NOT NULL DEFAULT 0, join_instance TEXT, role_version TEXT, source_status TEXT);
CREATE TABLE IF NOT EXISTS inputs (
    delivery_id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, sequence INTEGER NOT NULL,
    body TEXT NOT NULL, created_ns INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS context (
    meeting_id TEXT NOT NULL, sentence_key TEXT NOT NULL, body TEXT NOT NULL,
    suppressed INTEGER NOT NULL DEFAULT 0, generation INTEGER NOT NULL,
    PRIMARY KEY (meeting_id,sentence_key));
CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, generation INTEGER NOT NULL,
    snapshot TEXT NOT NULL, role_version TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending', result TEXT, created_ns INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS input_meeting ON inputs(meeting_id,sequence);
CREATE INDEX IF NOT EXISTS job_meeting ON jobs(meeting_id,generation);
CREATE TABLE IF NOT EXISTS outbox (
    uuid TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, kind TEXT NOT NULL,
    content TEXT NOT NULL, evidence TEXT NOT NULL, generation INTEGER NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending', error TEXT,
    created_ns INTEGER NOT NULL, sent_at REAL);
'''


def _tables(db):
    return {row['name'] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def _columns(db, table):
    return {row['name'] for row in db.execute(f'PRAGMA table_info({table})')}


def _validate_unversioned(db):
    tables = _tables(db)
    if not tables:
        return
    if not CORE_TABLES <= tables or not tables <= KNOWN_TABLES:
        raise StorageError('unsupported database schema; do not discard existing data')
    if any(not columns <= _columns(db, table) for table, columns in BASE_COLUMNS.items()):
        raise StorageError('unsupported database schema; do not discard existing data')
    if 'meeting_materials' in tables and 'outbox' not in tables:
        raise StorageError('unsupported database schema; do not discard existing data')


def _add_column(db, table, name):
    if name not in _columns(db, table):
        db.execute(f'ALTER TABLE {table} ADD COLUMN {name} TEXT')


def _validate_versioned(db, expected):
    if _tables(db) != set(expected):
        raise StorageError('unsupported database schema; do not discard existing data')
    if any(_columns(db, table) != columns for table, columns in expected.items()):
        raise StorageError('unsupported database schema; do not discard existing data')


def _to_v1(db):
    _validate_unversioned(db)
    # sqlite3.executescript() commits implicitly; execute statements separately so
    # the migration runner owns one atomic transaction.
    for statement in SCHEMA_V1.split(';'):
        if statement.strip():
            db.execute(statement)
    for name in ('join_instance', 'role_version', 'source_status'):
        _add_column(db, 'meetings', name)


def _to_v2(db):
    if _tables(db) == set(V2_COLUMNS) and all(
            _columns(db, table) == columns for table, columns in V2_COLUMNS.items()):
        return
    _validate_versioned(db, V1_COLUMNS)
    db.execute('''CREATE TABLE IF NOT EXISTS meeting_materials (
        meeting_id TEXT PRIMARY KEY, body TEXT NOT NULL, loaded_ns INTEGER NOT NULL)''')
    for table, names in (('meetings', ('material_version','material_state')),
                         ('jobs', ('context_version',)), ('outbox', ('context_version',))):
        for name in names:
            _add_column(db, table, name)


RUNNER = MigrationRunner([Migration(0, 1, _to_v1), Migration(1, 2, _to_v2)])


def migrate_adviser(path, connection: sqlite3.Connection, max_database_bytes):
    database = SQLiteDatabase(path, connection, readonly=False, max_database_bytes=max_database_bytes)
    RUNNER.migrate(database, target_version=ADVISER_SCHEMA_VERSION)
    _validate_versioned(connection, V2_COLUMNS)


def require_current_adviser_schema(connection: sqlite3.Connection):
    version = connection.execute('PRAGMA user_version').fetchone()[0]
    if version != ADVISER_SCHEMA_VERSION:
        raise StorageError('database version is not supported for read-only access; run a write command after checking backups')
    _validate_versioned(connection, V2_COLUMNS)
