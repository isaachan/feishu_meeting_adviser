"""Local JSONL caption input: same durable pipeline, no Feishu event polling.

Each input line is one JSON object: `text` (required non-empty string),
optional `id` (stable sentence identity), `speaker`, `language`,
`start_time`/`end_time` (timezone-bearing RFC3339 or integer milliseconds).
Unknown times stay unknown; they are never filled with the current time, and
captions without any time are context-only, never fresh meeting input.
"""
import json
import sys
import time

from .collector import Collector, PageFormatError, optional_string, raw_time, utc_now
from .infrastructure.errors import LifecycleError
from .infrastructure.file_lock import FileLock
from .models import Caption, IngestMode, SourceState, Status, digest, epoch_ns
from .storage import StorageError, Store


def parse_local_line(line: str, meeting_id: str, *, received_at: str, default_speaker: str) -> Caption:
    try:
        value = json.loads(line)
    except ValueError:
        raise PageFormatError('local caption line is not JSON; line not committed') from None
    if not isinstance(value, dict):
        raise PageFormatError('local caption line must be an object; line not committed')
    text = value.get('text')
    if not isinstance(text, str) or not text.strip():
        raise PageFormatError('local caption text missing; line not committed')
    sentence_id = optional_string(value.get('id'), 'local caption id')
    speaker = optional_string(value.get('speaker'), 'local caption speaker')
    if speaker is not None and not speaker.strip():
        raise PageFormatError('local caption speaker must be non-empty; line not committed')
    language = optional_string(value.get('language'), 'local caption language')
    start = raw_time(value.get('start_time'))
    end = raw_time(value.get('end_time'))
    if start is not None and epoch_ns(start, numeric_milliseconds=True) is None:
        raise PageFormatError('local caption start_time is not RFC3339 or integer milliseconds')
    if end is not None and epoch_ns(end, numeric_milliseconds=True) is None:
        raise PageFormatError('local caption end_time is not RFC3339 or integer milliseconds')
    speaker_id = speaker or default_speaker
    if sentence_id:
        key = 's:' + sentence_id
    else:
        key = 'h:' + digest([text, speaker_id, speaker_id, start, end])
    return Caption(meeting_id=meeting_id, sentence_key=key, text=text, received_at=received_at,
                   mode=IngestMode.LIVE, sentence_id=sentence_id, speaker_id=speaker_id,
                   speaker_name=speaker_id, language=language, start_time=start, end_time=end,
                   identity_degraded=not bool(sentence_id))


class LocalCaptionSource:
    """Feeds locally produced JSONL captions through the durable delivery pipeline.

    Requires a joined bot session for the meeting identity; join/leave and
    in-meeting replies still use the Feishu API. Holds the same application
    and meeting locks as the polling source, so the two input modes cannot
    run concurrently for one data directory and meeting.
    """

    def __init__(self, store: Store, manager, *, speaker: str = 'local', clock=time.time,
                 diagnose=None, on_caption=None, stream=None):
        if not isinstance(speaker, str) or not speaker.strip():
            raise LifecycleError('local speaker label must be a non-empty string')
        self.store = store
        self.manager = manager
        self.config = manager.config
        self.clock = clock
        self.session = manager.saved_meeting(require_joined=True)
        self.meeting_id = self.session['meeting_id']
        self.speaker = speaker
        self.diagnose = diagnose or (lambda record: None)
        self.on_caption = on_caption or (lambda kind, caption: None)
        self.stream = stream if stream is not None else sys.stdin
        self._app_lock = manager.application_lock()
        self._meeting_lock = FileLock(
            self.config.data_directory / 'locks' / ('meeting_' + digest(self.meeting_id) + '.lock'))

    def run(self) -> Status:
        self._app_lock.acquire()
        try:
            self._meeting_lock.acquire()
            try:
                return self._run_locked()
            finally:
                self._meeting_lock.release()
        finally:
            self._app_lock.release()

    def _publish(self, state: SourceState, reason: str) -> Status:
        status = Status(self.meeting_id, state, reason, utc_now(self.clock))
        self.store.publish_status(status)
        return status

    def _run_locked(self) -> Status:
        now = int(self.clock())
        start = max(1_000_000_000, now - self.config.scan.initial_lookback_seconds)
        fresh = {'schema': 1, 'initial_start': start, 'query_start': start, 'query_end': None,
                 'cursor': None, 'cursor_history': [], 'page_size': self.config.scan.page_size,
                 'mode': IngestMode.LIVE.value, 'completed_until': None, 'last_reconciled': None,
                 'last_success': None}
        checkpoint = self.store.ensure_meeting(self.meeting_id, fresh)
        Collector._validate_checkpoint(checkpoint)
        self._publish(SourceState.COLLECTING, 'local_input_open')
        try:
            while True:
                line = self.stream.readline()
                if not line:
                    break
                if line.strip():
                    self._commit_line(line.strip())
        except KeyboardInterrupt:
            return self._publish(SourceState.STOPPED, 'local_input_stopped')
        except PageFormatError:
            self._publish(SourceState.ACTION_REQUIRED, 'local_input_invalid')
            raise LifecycleError(
                'invalid local caption input; producer output rejected, nothing skipped') from None
        except (StorageError, OSError):
            self._publish(SourceState.ACTION_REQUIRED, 'local_storage')
            raise
        return self._publish(SourceState.STOPPED, 'local_input_closed')

    def _commit_line(self, line: str) -> None:
        received_at = utc_now(self.clock)
        caption = parse_local_line(line, self.meeting_id, received_at=received_at,
                                   default_speaker=self.speaker)
        checkpoint = dict(self.store.checkpoint(self.meeting_id))
        checkpoint['last_success'] = received_at
        for field, value in (('last_caption_event_ns', caption.event_ns),
                             ('last_caption_end_ns', caption.end_ns)):
            if value is not None:
                checkpoint[field] = max(value, checkpoint.get(field) or value)
        counts = self.store.commit_page(self.meeting_id, [caption], checkpoint)
        for kind, committed in self.store.last_commit_changes:
            self.on_caption(kind, committed)
        self.diagnose({'kind': 'local_caption', 'counts': counts, 'identity': caption.sentence_key[:2],
                       'end_receive_seconds': round(self.clock() - caption.end_ns / 1e9, 3)
                       if caption.end_ns is not None else None})

    def state(self) -> Status:
        status = self.store.current_state(self.meeting_id)
        return status or Status(self.meeting_id, SourceState.STOPPED, 'not_started', utc_now(self.clock))
