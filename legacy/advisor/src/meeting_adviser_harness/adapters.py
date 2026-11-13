"""Local transcript, material, and reply adapters for the production service."""
from datetime import datetime, timezone
from pathlib import Path
import time
from uuid import uuid4

from feishu_meeting_adviser.context_probe import UserContextClient, description_references
from feishu_meeting_adviser.materials import MaterialError
from feishu_meeting_adviser.models import Caption, SourceState, Status, digest
from feishu_meeting_adviser.oauth import OAuthManager


class MaterialAdapter:
    """Supply a local event and delegate referenced documents to user OAuth."""

    def __init__(self, config, oauth_directory: Path, description: str,
                 session: dict, stop_event):
        self.config, self.description, self.session = config, description, session
        self.timeout = config.timeout_seconds
        self.oauth_manager = None
        self.delegate = None
        if description_references(description):
            self.oauth_manager = OAuthManager(config.credentials, oauth_directory)
            self.delegate = UserContextClient(
                config.credentials,
                oauth_manager=self.oauth_manager,
                timeout=config.timeout_seconds,
                request_seconds=config.request_seconds,
                wait=lambda seconds: stop_event.wait(seconds),
            )

    def event(self, calendar_id, event_id, *, timeout=None):
        joined = int(self.session['joined_at'])
        return {
            'event_id': event_id,
            'status': 'confirmed',
            'summary': 'Harness meeting materials',
            'description': '',
            'description_rich': self.description,
            'attachments': [],
            'vchat': {'meeting_url': 'https://vc.feishu.cn/j/' + self.session['meeting_no']},
            'start_time': {'timestamp': str(joined - 60)},
            'end_time': {'timestamp': str(joined + 86400)},
        }

    def find_event(self, calendar_id, start, end, meeting_no, *, timeout=None):
        return {
            'matches': [{'event_id': 'harness'}],
            'truncated_possible': False,
        }

    def _read(self, name, token, *, timeout=None):
        if self.delegate is None:
            raise MaterialError(
                'document reference requires existing user OAuth authorization')
        return getattr(self.delegate, name)(token, timeout=timeout)

    def node(self, token, *, timeout=None):
        return self._read('node', token, timeout=timeout)

    def document(self, token, *, timeout=None):
        return self._read('document', token, timeout=timeout)

    def document_text(self, token, *, timeout=None):
        return self._read('document_text', token, timeout=timeout)


class ReplyAdapter:
    def __init__(self, display):
        self.display = display

    def send_message(self, meeting_id, text, message_uuid):
        self.display({
            'kind': 'reply',
            'status': 'allowed',
            'text': text,
            'uuid': message_uuid,
        })


class SessionAdapter:
    def __init__(self, store, config, session, display):
        self.store, self.config = store, config
        self._session = session
        self.client = ReplyAdapter(display)

    def session(self, store=None):
        return dict(self._session)

    def saved_meeting(self, *, require_joined=False):
        return dict(self._session)


class TranscriptAdapter:
    def __init__(self, store):
        self.store = store

    def start(self, meeting_id):
        self.store.ensure_meeting(meeting_id, {})
        self.store.publish_status(Status(
            meeting_id,
            SourceState.COLLECTING,
            'harness_running',
            datetime.now(timezone.utc).isoformat(),
        ))

    def submit(self, meeting_id, counter, speaker, text):
        at = datetime.now(timezone.utc).isoformat()
        key = f'harness:{counter}:{uuid4().hex}'
        caption = Caption(
            meeting_id,
            key,
            text,
            at,
            event_time=at,
            speaker_id='speaker-' + digest(speaker)[:16],
            speaker_name=speaker,
        )
        self.store.commit_page(meeting_id, [caption], {'last': counter})
        return key

    def close(self):
        self.store.close()
