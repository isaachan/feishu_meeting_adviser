"""Initialize and validate the only supported pre-release adviser schema."""
import sqlite3

from .sqlite import SQLiteDatabase, StorageError


ADVISER_SCHEMA_VERSION = 2
CURRENT_COLUMNS = {
    'meetings': {
        'meeting_id', 'generation', 'decided_generation', 'participation',
        'source_state', 'error', 'retry_at', 'cooldown_until', 'join_instance',
        'role_version', 'source_status', 'material_version', 'material_state',
    },
    'inputs': {'delivery_id', 'meeting_id', 'sequence', 'body', 'created_ns'},
    'context': {'meeting_id', 'sentence_key', 'body', 'suppressed', 'generation'},
    'jobs': {
        'job_id', 'meeting_id', 'generation', 'snapshot', 'role_version', 'state',
        'result', 'created_ns', 'context_version',
    },
    'outbox': {
        'uuid', 'meeting_id', 'kind', 'content', 'evidence', 'generation', 'state',
        'error', 'created_ns', 'sent_at', 'context_version',
    },
    'meeting_materials': {'meeting_id', 'body', 'loaded_ns'},
}

CURRENT_SCHEMA = (
    '''CREATE TABLE meetings (
        meeting_id TEXT PRIMARY KEY, generation INTEGER NOT NULL DEFAULT 0,
        decided_generation INTEGER NOT NULL DEFAULT 0,
        participation TEXT NOT NULL DEFAULT 'active', source_state TEXT, error TEXT,
        retry_at REAL NOT NULL DEFAULT 0, cooldown_until REAL NOT NULL DEFAULT 0,
        join_instance TEXT, role_version TEXT, source_status TEXT,
        material_version TEXT, material_state TEXT)''',
    '''CREATE TABLE inputs (
        delivery_id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, sequence INTEGER NOT NULL,
        body TEXT NOT NULL, created_ns INTEGER NOT NULL)''',
    '''CREATE TABLE context (
        meeting_id TEXT NOT NULL, sentence_key TEXT NOT NULL, body TEXT NOT NULL,
        suppressed INTEGER NOT NULL DEFAULT 0, generation INTEGER NOT NULL,
        PRIMARY KEY (meeting_id,sentence_key))''',
    '''CREATE TABLE jobs (
        job_id TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, generation INTEGER NOT NULL,
        snapshot TEXT NOT NULL, role_version TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending', result TEXT, created_ns INTEGER NOT NULL,
        context_version TEXT)''',
    'CREATE INDEX input_meeting ON inputs(meeting_id,sequence)',
    'CREATE INDEX job_meeting ON jobs(meeting_id,generation)',
    '''CREATE TABLE outbox (
        uuid TEXT PRIMARY KEY, meeting_id TEXT NOT NULL, kind TEXT NOT NULL,
        content TEXT NOT NULL, evidence TEXT NOT NULL, generation INTEGER NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending', error TEXT,
        created_ns INTEGER NOT NULL, sent_at REAL, context_version TEXT)''',
    '''CREATE TABLE meeting_materials (
        meeting_id TEXT PRIMARY KEY, body TEXT NOT NULL, loaded_ns INTEGER NOT NULL)''',
)


def _tables(db: sqlite3.Connection) -> set[str]:
    return {row['name'] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row['name'] for row in db.execute(f'PRAGMA table_info({table})')}


def require_current_adviser_schema(connection: sqlite3.Connection) -> None:
    version = connection.execute('PRAGMA user_version').fetchone()[0]
    if (version != ADVISER_SCHEMA_VERSION or _tables(connection) != set(CURRENT_COLUMNS)
            or any(_columns(connection, table) != columns
                   for table, columns in CURRENT_COLUMNS.items())):
        raise StorageError(
            'development database schema is unsupported; delete local development data '
            'and initialize again')


def initialize_adviser_schema(database: SQLiteDatabase) -> None:
    """Create the current schema only; never upgrade an existing non-current schema."""
    with database.transaction():
        version = database.connection.execute('PRAGMA user_version').fetchone()[0]
        if version or _tables(database.connection):
            require_current_adviser_schema(database.connection)
            return
        for statement in CURRENT_SCHEMA:
            database.connection.execute(statement)
        database.connection.execute(f'PRAGMA user_version={ADVISER_SCHEMA_VERSION}')
        require_current_adviser_schema(database.connection)
