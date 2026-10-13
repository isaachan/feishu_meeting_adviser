"""Bounded, checkpointed scans; response parsing never depends on spike code."""
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import time
import statistics
from typing import Protocol

from .client import ApiError, ErrorKind, StopRequested
from .models import Caption, IngestMode, SourceState, Status, digest, epoch_ns
from .storage import StorageError, Store


class PageFormatError(Exception):
    pass


class EventsClient(Protocol):
    def events(self, query: dict) -> dict: ...


def utc_now(clock=time.time) -> str:
    return datetime.fromtimestamp(clock(), timezone.utc).isoformat()


def optional_string(value, field: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise PageFormatError(f'invalid {field} type; page not committed')
    return value


def raw_time(value):
    if value is not None and (isinstance(value, bool) or not isinstance(value, (str, int))):
        raise PageFormatError('invalid time field type; page not committed')
    return value


@dataclass(frozen=True)
class ParsedPage:
    captions: list[Caption]
    has_more: bool
    cursor: str | None
    event_count: int
    unknown_times: int
    fallback_identities: int
    kinds: dict[str, int]
    departures: list[tuple[str, int | None]]


def parse_page(response: dict, meeting_id: str, *, received_at: str, mode: IngestMode) -> ParsedPage:
    data = response.get('data') if isinstance(response, dict) else None
    if not isinstance(data, dict) or not isinstance(data.get('events'), list) or type(data.get('has_more')) is not bool:
        raise PageFormatError('missing events or has_more; page not committed')
    more = data['has_more']
    cursor = optional_string(data.get('page_token'), 'page_token')
    if more and not cursor:
        raise PageFormatError('has_more without page_token; page not committed')
    captions = []
    departures = []
    unknown = fallbacks = 0
    kinds = {}
    for event in data['events']:
        if not isinstance(event, dict):
            raise PageFormatError('event must be an object; page not committed')
        kind = event.get('event_type')
        stamp = raw_time(event.get('event_time'))
        if epoch_ns(stamp) is None:
            unknown += 1
        if kind not in ('transcript_received', 'meeting.activity'):
            kinds['other'] = kinds.get('other', 0) + 1
            continue
        payload = event.get('payload')
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                raise PageFormatError('invalid activity payload JSON; content hidden') from None
        if not isinstance(payload, dict):
            raise PageFormatError('activity payload must be an object')
        meeting = payload.get('meeting')
        if isinstance(meeting, dict) and meeting.get('id') not in (None, meeting_id):
            raise PageFormatError('payload meeting mismatch; page not committed')
        subtype = payload.get('activity_event_type') if kind == 'meeting.activity' else kind
        label = subtype if subtype in ('transcript_received', 'participant_joined', 'participant_left', 'chat_received') else 'other'
        kinds[label] = kinds.get(label, 0) + 1
        if subtype == 'participant_left':
            left_items = payload.get('participant_left_items', [])
            if not isinstance(left_items, list):
                raise PageFormatError('participant_left_items must be a list')
            for item in left_items:
                if isinstance(item, dict) and isinstance(item.get('participant'), dict):
                    uid = item['participant'].get('id')
                    if isinstance(uid, str):
                        departed_at = epoch_ns(item.get('leave_time'), numeric_milliseconds=True) or epoch_ns(stamp)
                        departures.append((uid, departed_at))
        if subtype != 'transcript_received':
            continue
        if 'transcript_received_items' in payload:
            items = payload['transcript_received_items']
            if not isinstance(items, list):
                raise PageFormatError('transcript_received_items must be a list')
        elif isinstance(payload.get('text'), str):
            items = [payload]
        else:
            raise PageFormatError('unrecognized transcript payload; page not committed')
        eid = optional_string(event.get('event_id'), 'event_id')
        for index, item in enumerate(items):
            if not isinstance(item, dict) or not isinstance(item.get('text'), str):
                raise PageFormatError('transcript text missing; page not committed')
            sid = optional_string(item.get('sentence_id'), 'sentence_id')
            speaker = item.get('speaker')
            if speaker is not None and not isinstance(speaker, dict):
                raise PageFormatError('invalid speaker shape; page not committed')
            speaker = speaker or {}
            speaker_id = optional_string(speaker.get('id'), 'speaker.id')
            speaker_name = optional_string(speaker.get('user_name'), 'speaker.user_name')
            language = optional_string(item.get('language'), 'language')
            start = raw_time(item.get('start_time_ms'))
            end = raw_time(item.get('end_time_ms'))
            if sid:
                key = 's:' + sid
            elif eid:
                key = f'e:{eid}:{index}'
            else:
                # Include speaker identity and times, so separate equal phrases are not merged when distinguishable.
                key = 'h:' + digest([item['text'], speaker_id, speaker_name, start, end])
            degraded = not bool(sid)
            fallbacks += degraded
            captions.append(Caption(meeting_id=meeting_id, sentence_key=key, text=item['text'], received_at=received_at,
                                    mode=mode, sentence_id=sid, speaker_id=speaker_id, speaker_name=speaker_name,
                                    language=language, event_id=eid, event_time=stamp, start_time=start, end_time=end,
                                    identity_degraded=degraded))
    return ParsedPage(captions, more, cursor if more else None, len(data['events']), unknown, fallbacks, kinds, departures)


@dataclass(frozen=True)
class ScanSettings:
    initial_lookback_seconds: int = 600
    overlap_seconds: int = 120
    page_size: int = 100
    max_pages_per_cycle: int = 10
    reconcile_seconds: int = 60

    def __post_init__(self):
        bounds = {'initial_lookback_seconds': (1, 86400), 'overlap_seconds': (1, 3600),
                  'page_size': (20, 100), 'max_pages_per_cycle': (1, 100), 'reconcile_seconds': (1, 3600)}
        for name, (low, high) in bounds.items():
            if type(getattr(self, name)) is not int or not low <= getattr(self, name) <= high:
                raise ValueError(f'invalid scan setting: {name}')


class Collector:
    def __init__(self, store: Store, meeting_id: str, client: EventsClient, *, settings=ScanSettings(), clock=time.time, should_stop=None, page_status=None, diagnose=None):
        self.store = store
        self.meeting_id = meeting_id
        self.client = client
        self.settings = settings
        self.clock = clock
        self.should_stop = should_stop or (lambda: False)
        self.page_status = page_status or (lambda page: None)
        self.diagnose = diagnose or (lambda record: None)
        now = int(clock())
        start = max(1_000_000_000, now - settings.initial_lookback_seconds)
        checkpoint = {'schema': 1, 'initial_start': start, 'query_start': start, 'query_end': now,
                      'cursor': None, 'cursor_history': [], 'page_size': settings.page_size,
                      'mode': IngestMode.INITIAL.value, 'completed_until': None, 'last_reconciled': None,
                      'last_success': None}
        self.store.ensure_meeting(meeting_id, checkpoint)
        self._validate_checkpoint(self.store.checkpoint(meeting_id))

    @staticmethod
    def _validate_checkpoint(cp):
        if (not isinstance(cp, dict) or cp.get('schema') != 1 or
            any(type(cp.get(k)) is not int for k in ('initial_start', 'query_start', 'page_size')) or
            cp.get('query_end') is not None and type(cp['query_end']) is not int or
            cp.get('cursor') is not None and not isinstance(cp['cursor'], str) or
            not isinstance(cp.get('cursor_history'), list) or
            cp.get('mode') not in tuple(IngestMode)):
            raise StorageError('incompatible collector checkpoint; investigate before resetting')

    def _begin_window(self) -> dict:
        cp = self.store.checkpoint(self.meeting_id)
        self._validate_checkpoint(cp)
        if cp['query_end'] is not None:
            return cp
        now = max(int(self.clock()), cp['completed_until'])
        reconcile = cp.get('recovery_start') is not None or cp['last_reconciled'] is None or now - cp['last_reconciled'] >= self.settings.reconcile_seconds
        cp['query_start'] = cp['initial_start'] if reconcile else max(cp['initial_start'], cp['completed_until'] - self.settings.overlap_seconds)
        cp['query_end'] = now
        cp['mode'] = IngestMode.BACKFILL.value if reconcile else IngestMode.LIVE.value
        cp['page_size'] = self.settings.page_size
        cp['cursor_history'] = []
        self.store.commit_page(self.meeting_id, [], cp)
        return cp

    def cycle(self) -> list[dict]:
        summaries = []
        for _ in range(self.settings.max_pages_per_cycle):
            if self.should_stop():
                raise StopRequested()
            cp = self._begin_window()
            query = {'meeting_id': self.meeting_id, 'start_time': str(cp['query_start']),
                     'end_time': str(cp['query_end']), 'page_size': cp['page_size']}
            if cp['cursor']:
                query['page_token'] = cp['cursor']
            started = time.monotonic()
            response = self.client.events(query)
            request_ms = round((time.monotonic() - started) * 1000)
            processing_start = time.monotonic()
            received_at = utc_now(self.clock)
            page = parse_page(response, self.meeting_id, received_at=received_at, mode=IngestMode(cp['mode']))
            if page.has_more and (page.cursor == cp['cursor'] or page.cursor in cp['cursor_history']):
                raise PageFormatError('page_token loop; page not committed')
            if len(cp['cursor_history']) >= 10000:
                raise PageFormatError('scan page safety limit; page not committed')
            next_cp = dict(cp)
            next_cp['last_success'] = received_at
            for field, values in (('last_caption_event_ns', [c.event_ns for c in page.captions]),
                                  ('last_caption_end_ns', [c.end_ns for c in page.captions])):
                known = [v for v in values if v is not None]
                if cp.get(field) is not None:
                    known.append(cp[field])
                next_cp[field] = max(known) if known else None
            next_cp['cursor'] = page.cursor
            next_cp['cursor_history'] = cp['cursor_history'] + [page.cursor] if page.has_more else []
            if not page.has_more:
                next_cp['completed_until'] = max(cp['completed_until'] or cp['query_end'], cp['query_end'])
                if cp['mode'] in (IngestMode.INITIAL, IngestMode.BACKFILL):
                    next_cp['last_reconciled'] = int(self.clock())
                next_cp['query_end'] = None
                next_cp['cursor_reset_count'] = 0
            status = self.page_status(page)
            counts = self.store.commit_page(self.meeting_id, page.captions, next_cp, statuses=(status,) if status else ())
            def latency(values):
                known = sorted(values)
                return ({'count': len(known), 'min': known[0], 'median': statistics.median(known),
                         'p95': known[max(0, (len(known)*95+99)//100-1)], 'max': known[-1]} if known else {'count':0})
            now_ns = epoch_ns(received_at)
            emitted = self.store.last_commit_captions
            summary = {'kind': 'page', 'event_count': page.event_count, 'kinds': page.kinds,
                       'has_more': page.has_more, 'unknown_times': page.unknown_times,
                       'identity_degraded': page.fallback_identities, 'counts': counts,
                       'request_ms': request_ms, 'processing_ms': round((time.monotonic()-processing_start)*1000),
                       'scan_mode': cp['mode'], 'query_start': cp['query_start'], 'query_end': cp['query_end'],
                       'new_event_receive_seconds': latency([(now_ns-c.event_ns)/1e9 for c in emitted if c.event_ns is not None]),
                       'new_end_receive_seconds': latency([(now_ns-c.end_ns)/1e9 for c in emitted if c.end_ns is not None]),
                       'latest_caption_event_age_seconds': (now_ns-next_cp['last_caption_event_ns'])/1e9 if next_cp['last_caption_event_ns'] is not None else None,
                       'latest_caption_end_age_seconds': (now_ns-next_cp['last_caption_end_ns'])/1e9 if next_cp['last_caption_end_ns'] is not None else None}
            self.diagnose(summary)  # Each committed page has its own diagnostic even if a later page fails.
            summaries.append(summary)
            if status and status.state == SourceState.ACTION_REQUIRED:
                raise ApiError(ErrorKind.BOT_ABSENT)
            if not page.has_more:
                break
        return summaries

    def reset_cursor(self, reason: str) -> None:
        cp = self.store.checkpoint(self.meeting_id)
        status = Status(self.meeting_id, SourceState.INCOMPLETE, reason, utc_now(self.clock),
                        cp['query_start'], cp['query_end'] or int(self.clock()))
        cp['cursor'] = None
        cp['cursor_history'] = []
        cp['mode'] = IngestMode.BACKFILL.value
        # Preserve the original bounded query and every downstream acknowledgement.
        self.store.commit_page(self.meeting_id, [], cp, statuses=(status,))
