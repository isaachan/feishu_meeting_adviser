"""Initialize and validate the only supported pre-release adviser schema."""
import sqlite3

from .sqlite import SQLiteDatabase, StorageError


ADVISER_SCHEMA_VERSION = 4
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
        'answer_uuid', 'channel', 'recipient_id',
    },
    'meeting_materials': {'meeting_id', 'body', 'loaded_ns'},
    'local_sessions': {
        'meeting_id', 'title', 'description', 'phase', 'created_ns', 'started_at',
        'ended_ns',
    },
}

LOCAL_SESSIONS_TABLE = '''CREATE TABLE local_sessions (
        meeting_id TEXT PRIMARY KEY, title TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '', phase TEXT NOT NULL DEFAULT 'recording',
        created_ns INTEGER NOT NULL, started_at REAL NOT NULL, ended_ns INTEGER)'''

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
        created_ns INTEGER NOT NULL, sent_at REAL, context_version TEXT,
        answer_uuid TEXT NOT NULL, channel TEXT NOT NULL DEFAULT 'meeting',
        recipient_id TEXT)''',
    'CREATE INDEX outbox_answer ON outbox(answer_uuid)',
    '''CREATE TABLE meeting_materials (
        meeting_id TEXT PRIMARY KEY, body TEXT NOT NULL, loaded_ns INTEGER NOT NULL)''',
    LOCAL_SESSIONS_TABLE,
)


def _tables(db: sqlite3.Connection) -> set[str]:
    return {row['name'] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row['name'] for row in db.execute(f'PRAGMA table_info({table})')}


# Shapes accepted by the one-transaction upgrade path.
V3_COLUMNS = {table: columns for table, columns in CURRENT_COLUMNS.items()
              if table != 'local_sessions'}
V2_OUTBOX_COLUMNS = V3_COLUMNS['outbox'] - {'answer_uuid', 'channel', 'recipient_id'}


def _shape_matches(db: sqlite3.Connection, columns_by_table: dict) -> bool:
    return (_tables(db) == set(columns_by_table)
            and all(_columns(db, table) == columns
                    for table, columns in columns_by_table.items()))


def _is_v2(db: sqlite3.Connection) -> bool:
    if (db.execute('PRAGMA user_version').fetchone()[0] != 2
            or _tables(db) != set(V3_COLUMNS)):
        return False
    return (_columns(db, 'outbox') == V2_OUTBOX_COLUMNS
            and all(_columns(db, table) == columns
                    for table, columns in V3_COLUMNS.items() if table != 'outbox'))


def _is_v3(db: sqlite3.Connection) -> bool:
    return (db.execute('PRAGMA user_version').fetchone()[0] == 3
            and _shape_matches(db, V3_COLUMNS))


def require_current_adviser_schema(connection: sqlite3.Connection) -> None:
    version = connection.execute('PRAGMA user_version').fetchone()[0]
    if version in (2, 3) and (_is_v2(connection) or _is_v3(connection)):
        raise StorageError('adviser database requires v4 initialization; '
                           'run an advisor session command to upgrade')
    if (version != ADVISER_SCHEMA_VERSION
            or not _shape_matches(connection, CURRENT_COLUMNS)):
        raise StorageError(
            'development database schema is unsupported; delete local development data '
            'and initialize again'
        )


def initialize_adviser_schema(database: SQLiteDatabase) -> None:
    """Create the current schema or upgrade a verified v2/v3 database in one transaction."""
    with database.transaction():
        db = database.connection
        version = db.execute('PRAGMA user_version').fetchone()[0]
        if version == 2 and _is_v2(db):
            db.execute('ALTER TABLE outbox ADD COLUMN answer_uuid TEXT')
            db.execute('ALTER TABLE outbox ADD COLUMN channel TEXT NOT NULL DEFAULT \'meeting\'')
            db.execute('ALTER TABLE outbox ADD COLUMN recipient_id TEXT')
            db.execute('UPDATE outbox SET answer_uuid=uuid')
            db.execute('CREATE INDEX outbox_answer ON outbox(answer_uuid)')
            db.execute('PRAGMA user_version=3')
            version = 3
        if version == 3 and _is_v3(db):
            db.execute(LOCAL_SESSIONS_TABLE)
            db.execute(f'PRAGMA user_version={ADVISER_SCHEMA_VERSION}')
            require_current_adviser_schema(db)
            return
        if version or _tables(db):
            require_current_adviser_schema(db)
            return
        for statement in CURRENT_SCHEMA:
            db.execute(statement)
        db.execute(f'PRAGMA user_version={ADVISER_SCHEMA_VERSION}')
        require_current_adviser_schema(db)
