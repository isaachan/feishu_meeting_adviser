"""Interruptible recovery policy, separate from transport and page persistence."""
import random
import threading
from .client import ApiError, ErrorKind, StopRequested
from .collector import Collector, PageFormatError, utc_now
from .models import IngestMode, SourceState, Status
from .storage import StorageError


class RecoveryLoop:
    def __init__(self, collector: Collector, *, stop_event=None, poll_seconds=2, cooldown_seconds=30,
                 jitter=random.random, diagnose=None, before_cycle=None):
        self.collector = collector
        self.stop_event = stop_event or threading.Event()
        self.poll_seconds = poll_seconds
        self.cooldown_seconds = cooldown_seconds
        self.jitter = jitter
        self.diagnose = diagnose or (lambda record: None)
        self.before_cycle = before_cycle or (lambda: None)
        self.failures = 0
        self.outage_start = collector.store.checkpoint(collector.meeting_id).get('recovery_start')
        self.step_succeeded = False
        self.terminal = False
        self.last_status = None

    def status(self, state, reason, *, gap_start=None, gap_end=None):
        status = Status(self.collector.meeting_id, state, reason, utc_now(self.collector.clock), gap_start, gap_end)
        self.last_status = status
        self.collector.store.publish_status(status)
        return status

    def step(self) -> float:
        self.step_succeeded = False
        if self.stop_event.is_set():
            self.terminal = True
            return 0
        collector = self.collector
        try:
            reason = self.before_cycle()
            if reason:
                self.status(SourceState.ACTION_REQUIRED, reason)
                self.terminal = True
                return 0
            summaries = collector.cycle()
            cp = collector.store.checkpoint(collector.meeting_id)
            if self.outage_start is not None and not cp['cursor']:
                status = Status(collector.meeting_id, SourceState.INCOMPLETE, 'recovery_completeness_unverified',
                                utc_now(collector.clock), self.outage_start, int(collector.clock()))
                cp['recovery_start'] = None
                collector.store.commit_page(collector.meeting_id, [], cp, statuses=(status,))
                self.outage_start = None
            status = (Status(collector.meeting_id, SourceState.RECOVERING, 'backfill_in_progress',
                             utc_now(collector.clock), self.outage_start, int(collector.clock()))
                      if self.outage_start is not None else
                      collector.store.active_status(collector.meeting_id, utc_now(collector.clock)))
            collector.store.publish_status(status)
            self.last_status = status
            self.diagnose({'kind': 'health', **collector.store.statistics(collector.meeting_id)})
            self.failures = 0
            self.step_succeeded = True
            # Page budget exhaustion is continuation, not an idle poll.
            return 0 if collector.store.checkpoint(collector.meeting_id)['cursor'] else self.poll_seconds
        except StopRequested:
            self.stop_event.set()
            self.terminal = True
            return 0
        except ApiError as exc:
            self.diagnose({'kind': 'error', **exc.safe_details()})
            cp = collector.store.checkpoint(collector.meeting_id)
            if exc.code == 120001 and cp['cursor'] and cp.get('cursor_reset_count', 0) == 0:
                # Controlled parameter-error recovery, not a claim of an official expiry code.
                cp['cursor_reset_count'] = 1
                collector.store.commit_page(collector.meeting_id, [], cp)
                collector.reset_cursor('cursor_rejected_unverified')
                self.last_status = collector.store.current_state(collector.meeting_id)
                return 0
            if not exc.recoverable:
                self.status(SourceState.ACTION_REQUIRED, exc.kind.value)
                self.terminal = True
                return 0
            self.failures += 1
            if self.outage_start is None:
                self.outage_start = cp['completed_until'] or cp['query_start']
                cp['mode'] = IngestMode.BACKFILL.value
                cp['recovery_start'] = self.outage_start
                collector.store.commit_page(collector.meeting_id, [], cp)
                self.status(SourceState.RECOVERING, exc.kind.value, gap_start=self.outage_start)
            base = min(30, 2 ** self.failures) if self.failures <= 5 else self.cooldown_seconds
            return max(base * (1 + 0.2 * self.jitter()), exc.retry_after or 0)
        except PageFormatError:
            self.diagnose({'kind': 'error', 'error_kind': 'page_format'})
            self.status(SourceState.ACTION_REQUIRED, 'page_format')
            self.terminal = True
            return 0

    def run(self, *, max_successful_cycles=None) -> Status | None:
        successes = 0
        try:
            while not self.stop_event.is_set() and not self.terminal:
                delay = self.step()
                if self.step_succeeded:
                    successes += 1
                if max_successful_cycles is not None and successes >= max_successful_cycles:
                    break
                if self.stop_event.wait(delay):
                    break
        except StorageError:
            self.last_status = Status(self.collector.meeting_id, SourceState.ACTION_REQUIRED, 'local_storage', utc_now(self.collector.clock))
            self.diagnose({'kind': 'error', 'error_kind': 'local_storage', 'checkpoint_advanced': False})
            self.terminal = True
            raise
        return self.last_status
