"""Private SQLite connections, transactions and ordered schema migrations."""
from contextlib import contextmanager
from dataclasses import dataclass
import os
from pathlib import Path
import sqlite3
import stat
from typing import Callable, Iterable
from urllib.parse import quote


class StorageError(Exception):
    """Safe local error, never includes database contents or a sensitive path."""


class CapacityError(StorageError):
    pass


def private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir() or stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise StorageError('data directory must be a real directory with mode 700')


def _check_database_file(path: Path, *, required: bool) -> None:
    if required and not path.exists():
        raise StorageError('database does not exist; read-only operation cannot initialize it')
    if path.is_symlink():
        raise StorageError('database must not be a symlink')
    if path.exists() and (not path.is_file() or path.stat().st_mode & 0o077):
        raise StorageError('database must be a regular file with mode 600')


class SQLiteDatabase:
    def __init__(self, path: Path, connection: sqlite3.Connection, *, readonly: bool, max_database_bytes: int):
        self.path = Path(path)
        self.connection = connection
        self.readonly = readonly
        self.max_database_bytes = max_database_bytes

    @classmethod
    def open_and_migrate(cls, path: Path, *, max_database_bytes: int = 150_000_000):
        path = Path(path)
        private_directory(path.parent)
        _check_database_file(path, required=False)
        fd = os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        os.close(fd)
        db = sqlite3.connect(path, isolation_level=None, timeout=5)
        try:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=FULL')
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA secure_delete=ON')
            db.execute('PRAGMA wal_autocheckpoint=256')
            db.execute('PRAGMA journal_size_limit=1048576')
            page_size = db.execute('PRAGMA page_size').fetchone()[0]
            max_pages = max_database_bytes // page_size
            if max_pages < db.execute('PRAGMA page_count').fetchone()[0]:
                raise CapacityError('database already exceeds configured capacity; data preserved')
            db.execute(f'PRAGMA max_page_count={max_pages}')
            db.execute('PRAGMA busy_timeout=5000')
            return cls(path, db, readonly=False, max_database_bytes=max_database_bytes)
        except BaseException:
            db.close()
            raise

    @classmethod
    def open_readonly(cls, path: Path, *, max_database_bytes: int = 150_000_000):
        path = Path(path)
        _check_database_file(path, required=True)
        uri = 'file:' + quote(str(path.absolute()), safe='/') + '?mode=ro'
        db = sqlite3.connect(uri, uri=True, isolation_level=None, timeout=5)
        try:
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA query_only=ON')
            db.execute('PRAGMA foreign_keys=ON')
            db.execute('PRAGMA busy_timeout=5000')
            return cls(path, db, readonly=True, max_database_bytes=max_database_bytes)
        except BaseException:
            db.close()
            raise

    @contextmanager
    def transaction(self, *, capacity_check: Callable[[], None] | None = None):
        if self.readonly:
            raise StorageError('read-only database cannot start a write transaction')
        try:
            self.connection.execute('BEGIN IMMEDIATE')
            yield self.connection
            if capacity_check:
                capacity_check()
            self.connection.execute('COMMIT')
        except sqlite3.Error as exc:
            if self.connection.in_transaction:
                self.connection.execute('ROLLBACK')
            if getattr(exc, 'sqlite_errorcode', 0) & 255 == sqlite3.SQLITE_FULL:
                raise CapacityError('database capacity or disk space exhausted; transaction rolled back') from None
            raise StorageError('local database operation failed; check space and permissions') from None
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute('ROLLBACK')
            raise

    def close(self):
        self.connection.close()


@dataclass(frozen=True)
class Migration:
    from_version: int
    to_version: int
    apply: Callable[[sqlite3.Connection], None]


class MigrationRunner:
    """Runs an explicit, contiguous migration chain in one caller-owned lock."""

    def __init__(self, migrations: Iterable[Migration]):
        self.migrations = {migration.from_version: migration for migration in migrations}

    def migrate(self, database: SQLiteDatabase, *, target_version: int) -> None:
        if database.readonly:
            raise StorageError('read-only database cannot be migrated')
        with database.transaction():
            version = database.connection.execute('PRAGMA user_version').fetchone()[0]
            if not isinstance(version, int) or version < 0 or version > target_version:
                raise StorageError('unsupported database schema; do not discard existing data')
            while version < target_version:
                migration = self.migrations.get(version)
                if migration is None or migration.to_version <= version or migration.to_version > target_version:
                    raise StorageError('unsupported database schema; do not discard existing data')
                migration.apply(database.connection)
                version = migration.to_version
                database.connection.execute(f'PRAGMA user_version={version}')


def connect_private(path: Path, *, max_database_bytes: int = 150_000_000) -> sqlite3.Connection:
    """Compatibility bridge while legacy stores adopt SQLiteDatabase directly."""
    return SQLiteDatabase.open_and_migrate(path, max_database_bytes=max_database_bytes).connection
