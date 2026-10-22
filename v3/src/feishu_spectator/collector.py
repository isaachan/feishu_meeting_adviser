"""Spectator polling: tolerant event parsing and checkpointed cursor increments.

Spectator mode has no push; this loop polls ListMeetingEvents with the saved
page_token, commits each parsed page atomically, and prints stored items as
best-effort JSONL after the durable commit.
"""
import statistics
from dataclasses import dataclass, field
import json
import random
import re
import threading
import time
from typing import Callable, Protocol

from .client import ApiError, ErrorKind, StopRequested
from .models import (ChatMessage, EventKind, MeetingItem, ParticipantChange,
                     TranscriptItem, digest, epoch_ms, utc_now)
from .storage import Store

_TRANSIENT_ITEMS = {
    'transcript_received': ('transcript_received_items', EventKind.TRANSCRIPT),
    'chat_received': ('chat_received_items', EventKind.CHAT),
    'participant_joined': ('participant_joined_items', EventKind.PARTICIPANT_JOINED),
    'participant_left': ('participant_left_items', EventKind.PARTICIPANT_LEFT),
}
_SAFE_REASON = re.compile(r'[a-z_]{1,64}\Z')
_SAFE_LABEL = re.compile(r'[A-Za-z0-9_.:-]{1,64}\Z')


class PageFormatError(Exception):
    pass


class EventsClient(Protocol):
    def events(self, query: dict) -> dict: ...


def _optional_str(value, field_name: str) -> str | None:
    if value is not None and not isinstance(value, str):
        raise PageFormatError(f'invalid {field_name} type; page not committed')
    return value


def _raw_time(value, field_name: str):
    if value is not None and (isinstance(value, bool) or not isinstance(value, (str, int))):
        raise PageFormatError(f'invalid {field_name} type; page not committed')
    return value


def _activity_subtype(event: dict) -> tuple[str, dict]:
    """Return (activity_event_type, payload) for the three observed shapes."""
    direct = event.get('activity_event_type')
    if isinstance(direct, str):
        return direct, event
    payload = event.get('payload')
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            raise PageFormatError('invalid activity payload JSON; content hidden') from None
    if not isinstance(payload, dict):
        if isinstance(event.get('text'), str):
            return 'transcript_received', event
        raise PageFormatError('event payload must be an object')
    nested = payload.get('activity_event_type')
    if event.get('event_type') == 'meeting.activity' and isinstance(nested, str):
        return nested, payload
    if isinstance(nested, str):
        return nested, payload
    if event.get('event_type') == 'transcript_received':
        return 'transcript_received', payload
    raise PageFormatError('unrecognized event shape; page not committed')


def _transcript(meeting_id: str, item: dict, event_id: str | None, index: int, received_at: str) -> TranscriptItem:
    if not isinstance(item, dict) or not isinstance(item.get('text'), str):
        raise PageFormatError('transcript text missing; page not committed')
    sentence_id = _optional_str(item.get('sentence_id'), 'sentence_id')
    speaker = item.get('speaker')
    if speaker is not None and not isinstance(speaker, dict):
        raise PageFormatError('invalid speaker shape; page not committed')
    speaker = speaker or {}
    speaker_id = _optional_str(speaker.get('id'), 'speaker.id')
    speaker_name = _optional_str(speaker.get('user_name'), 'speaker.user_name')
    language = _optional_str(item.get('language'), 'language')
    start = _raw_time(item.get('start_time_ms'), 'start_time_ms')
    end = _raw_time(item.get('end_time_ms'), 'end_time_ms')
    if sentence_id:
        key = 's:' + sentence_id
    elif event_id:
        key = f'e:{event_id}:{index}'
    else:
        key = 'h:' + digest([item['text'], speaker_id, speaker_name, start, end])
    return TranscriptItem(meeting_id=meeting_id, item_key=key, text=item['text'],
                          received_at=received_at, sentence_id=sentence_id,
                          speaker_id=speaker_id, speaker_name=speaker_name,
                          language=language, start_time=start, end_time=end,
                          identity_degraded=not bool(sentence_id))


def _chat(meeting_id: str, item: dict, received_at: str) -> ChatMessage:
    if not isinstance(item, dict) or not isinstance(item.get('content'), str):
        raise PageFormatError('chat content missing; page not committed')
    message_id = _optional_str(item.get('message_id'), 'message_id')
    operator = item.get('operator')
    if operator is not None and not isinstance(operator, dict):
        raise PageFormatError('invalid operator shape; page not committed')
    operator = operator or {}
    sender_id = _optional_str(operator.get('id'), 'operator.id')
    sender_name = _optional_str(operator.get('user_name'), 'operator.user_name')
    message_type = item.get('message_type')
    if message_type is not None and type(message_type) is not int:
        raise PageFormatError('invalid message_type; page not committed')
    send_time = _raw_time(item.get('send_time'), 'send_time')
    key = 'm:' + message_id if message_id else 'h:' + digest(
        [item['content'], sender_id, send_time])
    return ChatMessage(meeting_id=meeting_id, item_key=key, content=item['content'],
                       received_at=received_at, message_id=message_id, sender_id=sender_id,
                       sender_name=sender_name, message_type=message_type, send_time=send_time)


def _participant(meeting_id: str, item: dict, direction: str, received_at: str) -> ParticipantChange:
    if not isinstance(item, dict) or not isinstance(item.get('participant'), dict):
        raise PageFormatError('participant item must carry a participant object')
    participant = item['participant']
    participant_id = _optional_str(participant.get('id'), 'participant.id')
    participant_name = _optional_str(participant.get('user_name'), 'participant.user_name')
    if direction == 'joined':
        at_time = _raw_time(item.get('join_time'), 'join_time')
        leave_reason = None
    else:
        at_time = _raw_time(item.get('leave_time'), 'leave_time')
        leave_reason = item.get('leave_reason')
        if leave_reason is not None and type(leave_reason) is not int:
            raise PageFormatError('invalid leave_reason; page not committed')
    key = 'p:' + digest([participant_id, direction, at_time])
    return ParticipantChange(meeting_id=meeting_id, item_key=key, direction=direction,
                             received_at=received_at, participant_id=participant_id,
                             participant_name=participant_name, at_time=at_time,
                             leave_reason=leave_reason)


@dataclass
class ParsedPage:
    items: list[MeetingItem] = field(default_factory=list)
    has_more: bool = False
    page_token: str | None = None
    event_count: int = 0
    kinds: dict[str, int] = field(default_factory=dict)
    # Safe shape metadata for diagnostics: field names and short enum labels only.
    data_keys: tuple[str, ...] = ()
    activity_types: tuple[str, ...] = ()


def parse_page(response: dict, meeting_id: str, *, received_at: str) -> ParsedPage:
    data = response.get('data') if isinstance(response, dict) else None
    if (not isinstance(data, dict) or not isinstance(data.get('events'), list)
            or type(data.get('has_more')) is not bool):
        raise PageFormatError('missing events or has_more; page not committed')
    has_more = data['has_more']
    page_token = _optional_str(data.get('page_token'), 'page_token')
    if has_more and not page_token:
        raise PageFormatError('has_more without page_token; page not committed')
    # The returned cursor is the resume point for the next incremental pull even
    # when has_more is false (manual: "首次全量，后续用 page_token 增量").
    page = ParsedPage(has_more=has_more, page_token=page_token,
                      event_count=len(data['events']),
                      data_keys=tuple(sorted(k for k in data if isinstance(k, str))))
    activity_types: set[str] = set()
    for event in data['events']:
        if not isinstance(event, dict):
            raise PageFormatError('event must be an object; page not committed')
        subtype, payload = _activity_subtype(event)
        if _SAFE_LABEL.fullmatch(subtype):
            activity_types.add(subtype)
        if subtype not in _TRANSIENT_ITEMS:
            page.kinds['other'] = page.kinds.get('other', 0) + 1
            continue
        meeting = payload.get('meeting')
        if isinstance(meeting, dict) and meeting.get('id') not in (None, meeting_id):
            raise PageFormatError('payload meeting mismatch; page not committed')
        items_field, kind = _TRANSIENT_ITEMS[subtype]
        event_id = _optional_str(event.get('event_id'), 'event_id')
        if 'transcript_received_items' == items_field and 'transcript_received_items' not in payload:
            items = [payload] if isinstance(payload.get('text'), str) else []
        else:
            items = payload.get(items_field)
            if items is None:
                items = []
            if not isinstance(items, list):
                raise PageFormatError(f'{items_field} must be a list')
        label = kind.value
        for index, item in enumerate(items):
            if kind is EventKind.TRANSCRIPT:
                page.items.append(_transcript(meeting_id, item, event_id, index, received_at))
            elif kind is EventKind.CHAT:
                page.items.append(_chat(meeting_id, item, received_at))
            else:
                direction = 'joined' if kind is EventKind.PARTICIPANT_JOINED else 'left'
                page.items.append(_participant(meeting_id, item, direction, received_at))
        page.kinds[label] = page.kinds.get(label, 0) + 1
    page.activity_types = tuple(sorted(activity_types))
    return page


@dataclass(frozen=True)
class ListenSettings:
    poll_seconds: float = 1.0
    cooldown_seconds: float = 30
    page_size: int = 100
    max_pages_per_cycle: int = 10

    def __post_init__(self):
        bounds = {'poll_seconds': (0.7, 30), 'cooldown_seconds': (5, 3600),
                  'page_size': (1, 100), 'max_pages_per_cycle': (1, 100)}
        for name, (low, high) in bounds.items():
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not low <= value <= high:
                raise ValueError(f'invalid listen setting: {name}')


class ListenerStopped(Exception):
    def __init__(self, state: str, reason: str):
        self.state = state
        self.reason = reason if _SAFE_REASON.fullmatch(reason or '') else 'unknown'
        super().__init__(f'{state}: {self.reason}')


def latency_summary(samples) -> dict:
    """median/p95/max of speech-end-to-receipt seconds over the last 200 captions."""
    known = sorted(value for value in samples or []
                   if isinstance(value, (int, float)) and value >= 0)
    if not known:
        return {'count': 0}
    middle = statistics.median(known)
    return {'count': len(known), 'median': round(middle, 1),
            'p95': round(known[max(0, (len(known) * 95 + 99) // 100 - 1)], 1),
            'max': round(known[-1], 1)}


class Spectator:
    """One meeting's polling session; the user stays in the meeting."""

    def __init__(self, store: Store, meeting_id: str, client: EventsClient, *,
                 settings=ListenSettings(), clock=time.time, jitter=random.random,
                 should_stop=None, diagnose=None, on_item=None):
        self.store = store
        self.meeting_id = meeting_id
        self.client = client
        self.settings = settings
        self.clock = clock
        self.jitter = jitter
        self.should_stop = should_stop or (lambda: False)
        self.diagnose = diagnose or (lambda record: None)
        self.on_item = on_item or (lambda item: None)
        self.failures = 0
        checkpoint = {'schema': 1, 'page_token': None, 'page_size': settings.page_size,
                      'cursor_reset_count': 0, 'last_success': None, 'pages': 0,
                      'started_at': utc_now(clock), 'latency_samples': []}
        self.checkpoint = store.ensure_meeting(meeting_id, checkpoint)

    def poll_once(self) -> dict:
        """Drain pending pages once; returns a safe summary."""
        pages = new_items = duplicates = 0
        kinds: dict[str, int] = {}
        for _ in range(self.settings.max_pages_per_cycle):
            if self.should_stop():
                raise StopRequested()
            checkpoint = self.store.checkpoint(self.meeting_id)
            query = {'meeting_id': self.meeting_id, 'page_size': checkpoint['page_size']}
            if checkpoint['page_token']:
                query['page_token'] = checkpoint['page_token']
            response = self.client.events(query)
            received_at = utc_now(self.clock)
            page = parse_page(response, self.meeting_id, received_at=received_at)
            if (page.has_more and page.page_token is not None
                    and page.page_token == checkpoint['page_token']):
                raise PageFormatError('page_token loop; page not committed')
            next_checkpoint = dict(checkpoint)
            next_checkpoint['page_token'] = page.page_token
            next_checkpoint['last_success'] = received_at
            next_checkpoint['pages'] = checkpoint['pages'] + 1
            next_checkpoint['last_event_count'] = page.event_count
            next_checkpoint['last_kinds'] = page.kinds
            next_checkpoint['last_activity_types'] = list(page.activity_types)
            if page.event_count == 0:
                next_checkpoint['last_data_keys'] = list(page.data_keys)
            commit = self.store.commit_page(self.meeting_id, page.items, next_checkpoint)
            if commit.new_item_list:
                # Exposure latency for new captions: speech end -> this machine.
                # A high median documents the server-side pull delay (v2 finding
                # across 25 meetings), not a local polling artifact.
                received_ms = epoch_ms(received_at)
                fresh = [received_ms - item.end_ms
                         for item in commit.new_item_list
                         if item.kind is EventKind.TRANSCRIPT
                         and item.end_ms is not None and received_ms is not None]
                if fresh:
                    with_samples = dict(next_checkpoint)
                    with_samples['latency_samples'] = (
                        list(next_checkpoint.get('latency_samples') or [])
                        + [round(value / 1000, 1) for value in fresh])[-200:]
                    self.store.commit_page(self.meeting_id, [], with_samples)
            pages += 1
            new_items += commit.new_items
            duplicates += commit.duplicates
            for label, count in page.kinds.items():
                kinds[label] = kinds.get(label, 0) + count
            # Output is best effort after the durable commit, never an acknowledgement.
            for item in commit.new_item_list:
                self.on_item(item)
            record = {'kind': 'page', 'event_count': page.event_count, 'kinds': page.kinds,
                      'has_more': page.has_more, 'new_items': commit.new_items,
                      'duplicates': commit.duplicates,
                      'activity_types': list(page.activity_types)}
            if page.event_count == 0:
                record['data_keys'] = list(page.data_keys)
            self.diagnose(record)
            if not page.has_more:
                break
        self.failures = 0
        return {'pages': pages, 'new_items': new_items, 'duplicates': duplicates, 'kinds': kinds}

    def run(self) -> None:
        """Poll until stop, terminal error, or an unrecoverable failure."""
        while not self.should_stop():
            try:
                summary = self.poll_once()
                if self.should_stop():
                    break
                delay = (0 if summary['pages'] >= self.settings.max_pages_per_cycle
                         else self.settings.poll_seconds)
                if self._sleep(delay):
                    break
            except StopRequested:
                break
            except ApiError as exc:
                self.diagnose({'kind': 'error', **exc.safe_details()})
                if exc.kind is ErrorKind.PARAMETER:
                    checkpoint = self.store.checkpoint(self.meeting_id)
                    if (exc.code == 120001 and checkpoint['page_token']
                            and checkpoint['cursor_reset_count'] == 0):
                        # One controlled re-pull without the cursor; idempotent
                        # storage absorbs duplicates. Not an official expiry claim.
                        checkpoint['cursor_reset_count'] = 1
                        checkpoint['page_token'] = None
                        self.store.commit_page(self.meeting_id, [], checkpoint)
                        continue
                if not exc.recoverable:
                    state = ('reauthorization_required' if exc.kind is ErrorKind.TOKEN
                             else 'permission_or_presence' if exc.kind is ErrorKind.DENIED
                             else exc.kind.value)
                    raise ListenerStopped(state, exc.kind.value) from exc
                self.failures += 1
                base = min(30, 2 ** self.failures) if self.failures <= 5 else self.settings.cooldown_seconds
                if self._sleep(max(base * (1 + 0.2 * self.jitter()), exc.retry_after or 0)):
                    break
            except PageFormatError:
                self.diagnose({'kind': 'error', 'error_kind': 'page_format'})
                raise ListenerStopped('page_format', 'page_format') from None

    def _sleep(self, seconds: float) -> bool:
        deadline = self.clock() + max(0.0, seconds)
        while not self.should_stop():
            if self.clock() >= deadline:
                return False
            time.sleep(min(0.1, max(0.0, deadline - self.clock())))
        return True
