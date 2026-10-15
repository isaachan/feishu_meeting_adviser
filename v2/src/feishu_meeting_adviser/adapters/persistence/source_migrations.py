"""Versioned source-store schema migrations and read-only validation."""
import json
import sqlite3

from .sqlite import Migration, MigrationRunner, SQLiteDatabase, StorageError


SOURCE_SCHEMA_VERSION = 5

V1_COLUMNS = {
    'meetings': {'meeting_id', 'checkpoint', 'state'},
    'captions': {'meeting_id', 'sentence_key', 'fingerprint', 'body'},
    'deliveries': {'sequence', 'meeting_id', 'delivery_id', 'kind', 'body', 'created_ns'},
    'consumers': {'meeting_id', 'consumer_id', 'acknowledged', 'offered'},
    'attempts': {'meeting_id', 'consumer_id', 'sequence', 'count'},
}
V2_COLUMNS = V1_COLUMNS | {
    'risks': {'meeting_id', 'identity', 'body'},
}
V3_COLUMNS = V2_COLUMNS | {
    'sessions': {'app_key', 'body'},
    'conflicts': {'meeting_id', 'sentence_key', 'delivery_id', 'resolved'},
}
V4_COLUMNS = V3_COLUMNS | {
    'memberships': {'meeting_id', 'active', 'left_at'},
    'tombstones': {'identity', 'deleted_at'},
}
V5_COLUMNS = V4_COLUMNS | {
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

SCHEMA_V1 = (
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
)


def _tables(db: sqlite3.Connection) -> set[str]:
    return {row['name'] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row['name'] for row in db.execute(f'PRAGMA table_info({table})')}


def _validate(db: sqlite3.Connection, expected: dict[str, set[str]]) -> None:
    if _tables(db) != set(expected):
        raise StorageError('unsupported database schema; do not discard existing data')
    if any(_columns(db, table) != columns for table, columns in expected.items()):
        raise StorageError('unsupported database schema; do not discard existing data')
    indexes = {row['name'] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")}
    if 'deliveries_meeting' not in indexes:
        raise StorageError('unsupported database schema; do not discard existing data')


def _to_v1(db: sqlite3.Connection) -> None:
    if _tables(db):
        raise StorageError('unsupported database schema; do not discard existing data')
    for statement in SCHEMA_V1:
        db.execute(statement)


def _to_v2(db: sqlite3.Connection) -> None:
    _validate(db, V1_COLUMNS)
    db.execute('''CREATE TABLE risks (
        meeting_id TEXT NOT NULL REFERENCES meetings(meeting_id), identity TEXT NOT NULL,
        body TEXT NOT NULL, PRIMARY KEY (meeting_id, identity))''')


def _decode_object(body: str) -> dict:
    try:
        value = json.loads(body)
    except (TypeError, ValueError):
        value = None
    if not isinstance(value, dict):
        raise StorageError('invalid stored record; body hidden, investigate without deleting data')
    return value


def _to_v3(db: sqlite3.Connection) -> None:
    _validate(db, V2_COLUMNS)
    db.execute('CREATE TABLE sessions (app_key TEXT PRIMARY KEY, body TEXT NOT NULL)')
    db.execute('''CREATE TABLE conflicts (
        meeting_id TEXT NOT NULL, sentence_key TEXT NOT NULL,
        delivery_id TEXT PRIMARY KEY REFERENCES deliveries(delivery_id),
        resolved INTEGER NOT NULL DEFAULT 0)''')
    for row in db.execute(
            "SELECT meeting_id, delivery_id, body FROM deliveries WHERE kind='conflict'").fetchall():
        candidate = _decode_object(row['body'])
        sentence_key = candidate.get('sentence_key')
        if not isinstance(sentence_key, str) or not sentence_key:
            raise StorageError('invalid stored conflict; body hidden')
        db.execute(
            'INSERT OR IGNORE INTO conflicts(meeting_id,sentence_key,delivery_id) VALUES (?,?,?)',
            (row['meeting_id'], sentence_key, row['delivery_id']),
        )


def _record_memberships(db: sqlite3.Connection) -> None:
    for row in db.execute('SELECT body FROM sessions').fetchall():
        session = _decode_object(row['body'])
        meeting_id = session.get('meeting_id')
        phase = session.get('phase')
        if not meeting_id or phase not in ('joined', 'left', 'leave_uncertain'):
            continue
        db.execute(
            '''INSERT INTO memberships(meeting_id,active,left_at) VALUES (?,?,?)
               ON CONFLICT(meeting_id) DO UPDATE SET
               active=excluded.active,left_at=excluded.left_at''',
            (meeting_id, int(phase != 'left'), session.get('left_at') or session.get('manual_confirmation_at')),
        )


def _to_v4(db: sqlite3.Connection) -> None:
    _validate(db, V3_COLUMNS)
    db.execute('''CREATE TABLE memberships (
        meeting_id TEXT PRIMARY KEY, active INTEGER NOT NULL, left_at TEXT)''')
    db.execute('CREATE TABLE tombstones (identity TEXT PRIMARY KEY, deleted_at TEXT NOT NULL)')
    _record_memberships(db)


def _expression(prefix: str, columns: tuple[str, ...]) -> str:
    return ' + '.join(f'COALESCE(length(CAST({prefix}{column} AS BLOB)),0)' for column in columns)


def _create_accounting(db: sqlite3.Connection) -> None:
    db.execute('''CREATE TABLE accounting (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), bytes INTEGER NOT NULL)''')
    db.execute('INSERT INTO accounting(singleton,bytes) VALUES (1,0)')
    total = 0
    for table, columns in TRACKED_COLUMNS.items():
        old = _expression('OLD.', columns)
        new = _expression('NEW.', columns)
        for action, expression in (
                ('insert', new), ('delete', f'-({old})'), ('update', f'({new}) - ({old})')):
            db.execute(
                f'''CREATE TRIGGER account_{table}_{action} AFTER {action.upper()} ON {table}
                    BEGIN UPDATE accounting SET bytes=bytes+({expression}) WHERE singleton=1; END''')
        total += db.execute(
            f'SELECT COALESCE(SUM({_expression("", columns)}),0) FROM {table}').fetchone()[0]
    db.execute('UPDATE accounting SET bytes=? WHERE singleton=1', (total,))


def _to_v5(db: sqlite3.Connection) -> None:
    _validate(db, V4_COLUMNS)
    # A v4 database was never released, but old startup code accepted it. Re-read
    # sessions so a partially prepared transitional database remains compatible.
    _record_memberships(db)
    _create_accounting(db)


def _required_schema_objects(db: sqlite3.Connection) -> None:
    triggers = {row['name'] for row in db.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'")}
    expected_triggers = {
        f'account_{table}_{action}'
        for table in TRACKED_COLUMNS
        for action in ('insert', 'delete', 'update')
    }
    if triggers != expected_triggers:
        raise StorageError('unsupported database schema; do not discard existing data')
    row = db.execute('SELECT bytes FROM accounting WHERE singleton=1').fetchone()
    if row is None or not isinstance(row['bytes'], int) or row['bytes'] < 0:
        raise StorageError('unsupported database schema; do not discard existing data')


RUNNER = MigrationRunner([
    Migration(0, 1, _to_v1),
    Migration(1, 2, _to_v2),
    Migration(2, 3, _to_v3),
    Migration(3, 4, _to_v4),
    Migration(4, 5, _to_v5),
])


def migrate_source(path, connection: sqlite3.Connection, max_database_bytes: int) -> None:
    database = SQLiteDatabase(path, connection, readonly=False, max_database_bytes=max_database_bytes)
    RUNNER.migrate(database, target_version=SOURCE_SCHEMA_VERSION)
    _validate(connection, V5_COLUMNS)
    _required_schema_objects(connection)


def require_current_source_schema(connection: sqlite3.Connection) -> None:
    version = connection.execute('PRAGMA user_version').fetchone()[0]
    if version != SOURCE_SCHEMA_VERSION:
        raise StorageError(
            'database version is not supported for read-only access; run a write command after checking backups')
    _validate(connection, V5_COLUMNS)
    _required_schema_objects(connection)
