"""One configuration-aware construction path for managed SQLite stores."""
from dataclasses import dataclass
from pathlib import Path

from ...config import Configuration
from .adviser_store import AdviserStore
from .source_store import DurableFakeConsumer, Store


@dataclass(frozen=True)
class StorageFactory:
    config: Configuration
    root: Path | None = None

    @property
    def directory(self):
        return Path(self.root or self.config.data_directory)

    @property
    def source_path(self):
        return self.directory / 'source.sqlite3'

    @property
    def adviser_path(self):
        return self.directory / 'adviser.sqlite3'

    def initialize_source(self):
        if self.source_path.exists():
            store = self.source()
            store.close()
            return
        Store.initialize(
            self.source_path,
            max_database_bytes=self.config.database_capacity_bytes,
        )

    def initialize_adviser(self):
        AdviserStore.initialize(
            self.adviser_path,
            max_database_bytes=self.config.database_capacity_bytes,
        )

    def source(self, *, readonly=False):
        constructor = Store.open_readonly if readonly else Store
        return constructor(self.source_path,
            max_payload_bytes=self.config.content_capacity_bytes,
            max_database_bytes=self.config.database_capacity_bytes)

    def adviser(self, *, readonly=False):
        constructor = AdviserStore.open_readonly if readonly else AdviserStore
        return constructor(self.adviser_path,
            max_payload_bytes=self.config.content_capacity_bytes,
            max_database_bytes=self.config.database_capacity_bytes)

    def fake_consumer(self, identity):
        return DurableFakeConsumer(self.directory / 'fake_consumers' / (identity + '.sqlite3'),
            max_database_bytes=self.config.database_capacity_bytes)
