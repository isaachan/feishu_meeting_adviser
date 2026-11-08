"""Explicit local meeting sessions with no Feishu bot dependency.

A local session is created by the user, identified by its own ``local:`` meeting
id and owns its timeline. The manager exposes the small session interface the
adviser service consumes; replies are published to the local outbox only.
"""
from .adapters.persistence.adviser_store import AdviserStore
from .config import Configuration
from .infrastructure.errors import LifecycleError
from .storage import StorageError


class LocalReplyClient:
    """Local sessions have no remote reply transport; the outbox is the result sink."""

    def send_message(self, meeting_id, text, message_uuid):
        raise LifecycleError(
            'local sessions publish replies locally; no meeting transport is configured')

    def send_direct_message(self, recipient_id, text, message_uuid):
        raise LifecycleError(
            'local sessions publish replies locally; no IM transport is configured')


class LocalSessionManager:
    """Session provider for one explicitly created local meeting session."""

    def __init__(self, data: AdviserStore, config: Configuration, meeting_id: str):
        self.data = data
        self.config = config
        self.meeting_id = meeting_id

    def _require(self) -> dict:
        try:
            return self.data.require_local_session(self.meeting_id)
        except StorageError as exc:
            raise LifecycleError(str(exc)) from None

    def session(self, store=None) -> dict:
        # Stable identity view: draining or ending must not look like a new meeting,
        # so volatile lifecycle fields are read through phase() instead.
        current = self._require()
        return {key: current[key] for key in
                ('source', 'meeting_id', 'meeting_no', 'join_instance', 'bot_user_id')}

    def saved_meeting(self, *, require_joined=False) -> dict:
        current = self._require()
        if require_joined and current['phase'] == 'ended':
            raise LifecycleError(
                'local session has ended; create a new session for the next meeting')
        return dict(current)

    def phase(self) -> str:
        return self._require()['phase']

    def mark_ended(self, *, now=None) -> dict:
        try:
            return self.data.set_local_session_phase(self.meeting_id, 'ended', now=now)
        except StorageError as exc:
            raise LifecycleError(str(exc)) from None

    @property
    def client(self) -> LocalReplyClient:
        return LocalReplyClient()
