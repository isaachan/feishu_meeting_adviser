"""Retention and explicit deletion include the stock fake consumer's local copies."""
from pathlib import Path
import time
from .collector import utc_now
from .infrastructure.errors import LifecycleError
from .infrastructure.file_lock import FileLock
from .models import digest
from .storage import DurableFakeConsumer
from .materials import ContentPreviews
from .adapters.persistence.factory import StorageFactory


class Maintenance:
    def __init__(self, store, config, *, clock=time.time, diagnose=None):
        self.store = store
        self.config = config
        self.clock = clock
        self.diagnose = diagnose or (lambda record:None)
        self.last_run = None
        self.storage_factory = StorageFactory(config)

    def lock(self):
        return FileLock(self.config.data_directory / 'locks' / 'maintenance.lock')

    def _clean_consumer_copies(self, *, strict_runtime=False):
        identities = self.store.deleted_identities()
        if not identities:
            return
        ContentPreviews.clean(self.config.data_directory/'content_previews',identities)
        from .opencode_backend import clean_runtime
        for identity in identities:
            clean_runtime(self.config.data_directory/'model_runtime'/'opencode'/identity,strict=strict_runtime)
        for path in (self.config.data_directory / 'fake_consumers').glob('*.sqlite3'):
            consumer = DurableFakeConsumer(path, max_database_bytes=self.config.database_capacity_bytes)
            try:
                consumer.forget_deleted_meetings(identities)
            finally:
                consumer.close()
        path = self.config.data_directory / 'adviser.sqlite3'
        if path.exists():
            data = self.storage_factory.adviser()
            try:
                for mid in data.list_meeting_ids():
                    if digest(mid) in identities:
                        data.forget_meeting(mid)
            finally:
                data.close()

    def _protected_model_meetings(self):
        path = self.config.data_directory / 'adviser.sqlite3'
        if not path.exists():
            return set()
        data = self.storage_factory.adviser()
        try:
            return data.protected_meetings()
        finally:
            data.close()

    def prune(self):
        with self.lock():
            if (self.config.data_directory / 'adviser.sqlite3').exists():
                self.storage_factory.initialize_adviser()
            cutoff = int((self.clock() - self.config.retention_days * 86400) * 1_000_000_000)
            result = self.store.prune(cutoff, at=utc_now(self.clock),protected_meetings=self._protected_model_meetings())
            self._clean_consumer_copies()
            ContentPreviews.clean(self.config.data_directory/'content_previews',cutoff_ns=cutoff)
            path = self.config.data_directory / 'adviser.sqlite3'
            if path.exists():
                data = self.storage_factory.adviser()
                try:
                    data.prune_history(cutoff)
                finally:
                    data.close()
            self.diagnose({'kind':'maintenance', **result})
            self.last_run = self.clock()
            return result

    def run_due(self):
        if self.last_run is None or self.clock() - self.last_run >= 60:
            try:
                self.prune()
            except LifecycleError:
                pass  # Another local consume/maintenance operation owns the short-lived lock.

    def delete_meeting(self, meeting_id):
        with FileLock(self.config.data_directory / 'locks' / 'adviser.lock'), self.lock():
            if (self.config.data_directory / 'adviser.sqlite3').exists():
                self.storage_factory.initialize_adviser()
            self.store.forget_meeting(meeting_id, at=utc_now(self.clock))
            self._clean_consumer_copies(strict_runtime=True)
