"""Retention and explicit deletion include the stock fake consumer's local copies."""
from pathlib import Path
import time
from .collector import utc_now
from .lifecycle import FileLock, LifecycleError
from .models import digest
from .storage import DurableFakeConsumer
from .adviser_store import AdviserStore
from .materials import ContentPreviews


class Maintenance:
    def __init__(self, store, config, *, clock=time.time, diagnose=None):
        self.store = store
        self.config = config
        self.clock = clock
        self.diagnose = diagnose or (lambda record:None)
        self.last_run = None

    def lock(self):
        return FileLock(self.config.data_directory / 'locks' / 'maintenance.lock')

    def _clean_consumer_copies(self):
        identities = {r[0] for r in self.store.db.execute('SELECT identity FROM tombstones')}
        if not identities:
            return
        ContentPreviews.clean(self.config.data_directory/'content_previews',identities)
        for path in (self.config.data_directory / 'fake_consumers').glob('*.sqlite3'):
            consumer = DurableFakeConsumer(path, max_database_bytes=self.config.database_capacity_bytes)
            try:
                consumer.forget_deleted_meetings(identities)
            finally:
                consumer.close()
        path = self.config.data_directory / 'adviser.sqlite3'
        if path.exists():
            data = AdviserStore(path,max_database_bytes=self.config.database_capacity_bytes)
            try:
                mids = [r['meeting_id'] for r in data.db.execute('SELECT meeting_id FROM meetings')]
                for mid in mids:
                    if digest(mid) in identities:
                        data.forget_meeting(mid)
            finally:
                data.close()

    def _protected_model_meetings(self):
        path = self.config.data_directory / 'adviser.sqlite3'
        if not path.exists():
            return set()
        data = AdviserStore(path,max_database_bytes=self.config.database_capacity_bytes)
        try:
            return data.protected_meetings()
        finally:
            data.close()

    def prune(self):
        with self.lock():
            cutoff = int((self.clock() - self.config.retention_days * 86400) * 1_000_000_000)
            result = self.store.prune(cutoff, at=utc_now(self.clock),protected_meetings=self._protected_model_meetings())
            self._clean_consumer_copies()
            ContentPreviews.clean(self.config.data_directory/'content_previews',cutoff_ns=cutoff)
            path = self.config.data_directory / 'adviser.sqlite3'
            if path.exists():
                data = AdviserStore(path,max_database_bytes=self.config.database_capacity_bytes)
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
            self.store.forget_meeting(meeting_id, at=utc_now(self.clock))
            self._clean_consumer_copies()
