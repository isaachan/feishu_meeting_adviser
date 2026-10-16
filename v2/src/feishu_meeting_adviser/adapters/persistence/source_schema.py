"""Initialize and validate the only supported pre-release source schema."""
import sqlite3

from .sqlite import SQLiteDatabase, StorageError


SOURCE_SCHEMA_VERSION = 5

CURRENT_COLUMNS = {
    'meetings': {'meeting_id', 'checkpoint', 'state'},
    'captions': {'meeting_id', 'sentence_key', 'fingerprint', 'body'},
    'deliveries': {'sequence', 'meeting_id', 'delivery_id', 'kind', 'body', 'created_ns'},
    'consumers': {'meeting_id', 'consumer_id', 'acknowledged', 'offered'},
    'attempts': {'meeting_id', 'consumer_id', 'sequence', 'count'},
    'risks': {'meeting_id', 'identity', 'body'},
    'sessions': {'app_key', 'body'},
    'conflicts': {'meeting_id', 'sentence_key', 'delivery_id', 'resolved'},
    'memberships': {'meeting_id', 'active', 'left_at'},
    'tombstones': {'identity', 'deleted_at'},
    'accounting': {'singleton', 'bytes'},
}

TRACKED_COLUMNS = {
    'meetings': ('checkpoint', 'state'),
    'captions': ('body', 'sentence_key', 'fingerprint'),
    'deliveries': ('body', 'delivery_id'),
    'risks': ('body',),
    'sessions': ('body',),
    'consumers': ('consumer_id',),
    'tombstones': ('identity', 'deleted_at'),
    'memberships': ('meeting_id', 'left_at'),
}

CURRENT_SCHEMA = (
    '''CREATE TABLE meetings (
        meeting_id TEXT PRIMARY KEY, checkpoint TEXT NOT NULL, state TEXT)''',
    '''CREATE TABLE captions (
        meeting_id TEXT NOT NULL REFERENCES meetings(meeting_id),
        sentence_key TEXT NOT NULL, fingerprint TEXT NOT NULL, body TEXT NOT NULL,
        PRIMARY KEY (meeting_id, sentence_key))''',
    '''CREATE TABLE deliveries (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        meeting_id TEXT NOT NULL REFERENCES meetings(meeting_id),
        delivery_id TEXT NOT NULL UNIQUE, kind TEXT NOT NULL, body TEXT NOT NULL,
        created_ns INTEGER NOT NULL)''',
    'CREATE INDEX deliveries_meeting ON deliveries(meeting_id, sequence)',
    '''CREATE TABLE consumers (
        meeting_id TEXT NOT NULL REFERENCES meetings(meeting_id), consumer_id TEXT NOT NULL,
        acknowledged INTEGER NOT NULL DEFAULT 0, offered INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (meeting_id, consumer_id))''',
    '''CREATE TABLE attempts (
        meeting_id TEXT NOT NULL, consumer_id TEXT NOT NULL,
        sequence INTEGER NOT NULL REFERENCES deliveries(sequence) ON DELETE CASCADE,
        count INTEGER NOT NULL, PRIMARY KEY (meeting_id, consumer_id, sequence))''',
    '''CREATE TABLE risks (
        meeting_id TEXT NOT NULL REFERENCES meetings(meeting_id), identity TEXT NOT NULL,
        body TEXT NOT NULL, PRIMARY KEY (meeting_id, identity))''',
    'CREATE TABLE sessions (app_key TEXT PRIMARY KEY, body TEXT NOT NULL)',
    '''CREATE TABLE conflicts (
        meeting_id TEXT NOT NULL, sentence_key TEXT NOT NULL,
        delivery_id TEXT PRIMARY KEY REFERENCES deliveries(delivery_id),
        resolved INTEGER NOT NULL DEFAULT 0)''',
    '''CREATE TABLE memberships (
        meeting_id TEXT PRIMARY KEY, active INTEGER NOT NULL, left_at TEXT)''',
    'CREATE TABLE tombstones (identity TEXT PRIMARY KEY, deleted_at TEXT NOT NULL)',
    '''CREATE TABLE accounting (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), bytes INTEGER NOT NULL)''',
    'INSERT INTO accounting(singleton,bytes) VALUES (1,0)',
)


def _tables(db: sqlite3.Connection) -> set[str]:
    return {row['name'] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row['name'] for row in db.execute(f'PRAGMA table_info({table})')}


def _expression(prefix: str, columns: tuple[str, ...]) -> str:
    return ' + '.join(
        f'COALESCE(length(CAST({prefix}{column} AS BLOB)),0)' for column in columns)


def _create_accounting_triggers(db: sqlite3.Connection) -> None:
    for table, columns in TRACKED_COLUMNS.items():
        old = _expression('OLD.', columns)
        new = _expression('NEW.', columns)
        for action, expression in (
                ('insert', new), ('delete', f'-({old})'), ('update', f'({new}) - ({old})')):
            db.execute(
                f'''CREATE TRIGGER account_{table}_{action} AFTER {action.upper()} ON {table}
                    BEGIN UPDATE accounting SET bytes=bytes+({expression}) WHERE singleton=1; END''')


def require_current_source_schema(connection: sqlite3.Connection) -> None:
    version = connection.execute('PRAGMA user_version').fetchone()[0]
    if version != SOURCE_SCHEMA_VERSION or _tables(connection) != set(CURRENT_COLUMNS):
        raise StorageError(
            'development database schema is unsupported; delete local development data '
            'and initialize again')
    if any(_columns(connection, table) != columns
           for table, columns in CURRENT_COLUMNS.items()):
        raise StorageError(
            'development database schema is unsupported; delete local development data '
            'and initialize again')
    indexes = {row['name'] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")}
    triggers = {row['name'] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'")}
    expected_triggers = {
        f'account_{table}_{action}'
        for table in TRACKED_COLUMNS
        for action in ('insert', 'delete', 'update')
    }
    row = connection.execute('SELECT bytes FROM accounting WHERE singleton=1').fetchone()
    if ('deliveries_meeting' not in indexes or triggers != expected_triggers
            or row is None or not isinstance(row['bytes'], int) or row['bytes'] < 0):
        raise StorageError(
            'development database schema is unsupported; delete local development data '
            'and initialize again')


def initialize_source_schema(database: SQLiteDatabase) -> None:
    """Create the current schema only; never upgrade an existing non-current schema."""
    with database.transaction():
        version = database.connection.execute('PRAGMA user_version').fetchone()[0]
        if version or _tables(database.connection):
            require_current_source_schema(database.connection)
            return
        for statement in CURRENT_SCHEMA:
            database.connection.execute(statement)
        _create_accounting_triggers(database.connection)
        database.connection.execute(f'PRAGMA user_version={SOURCE_SCHEMA_VERSION}')
        require_current_source_schema(database.connection)
