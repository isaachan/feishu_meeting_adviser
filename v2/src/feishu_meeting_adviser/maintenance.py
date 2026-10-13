"""Retention and explicit deletion include the stock fake consumer's local copies."""
from pathlib import Path
import time
from .collector import utc_now
from .lifecycle import FileLock, LifecycleError
from .models import digest
from .storage import DurableFakeConsumer


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
        for path in (self.config.data_directory / 'fake_consumers').glob('*.sqlite3'):
            consumer = DurableFakeConsumer(path, max_database_bytes=self.config.database_capacity_bytes)
            try:
                consumer.forget_deleted_meetings(identities)
            finally:
                consumer.close()

    def prune(self):
        with self.lock():
            cutoff = int((self.clock() - self.config.retention_days * 86400) * 1_000_000_000)
            result = self.store.prune(cutoff, at=utc_now(self.clock))
            self._clean_consumer_copies()
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
        with self.lock():
            self.store.forget_meeting(meeting_id, at=utc_now(self.clock))
            self._clean_consumer_copies()
