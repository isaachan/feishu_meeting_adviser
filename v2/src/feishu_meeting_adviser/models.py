"""Immutable input and delivery contracts. No upstream revision order is invented."""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
import re
from typing import Protocol, Sequence


class IngestMode(StrEnum):
    LIVE = 'live'
    INITIAL = 'initial_replay'
    BACKFILL = 'recovery_backfill'


class ChangeKind(StrEnum):
    ADDED = 'added'
    UPDATED = 'updated'
    CONFLICT = 'conflict'
    STATUS = 'status'


class SourceState(StrEnum):
    COLLECTING = 'collecting'
    RECOVERING = 'recovering'
    INCOMPLETE = 'incomplete'
    ACTION_REQUIRED = 'action_required'
    STOPPED = 'stopped'
    ENDED = 'ended'


RawTime = str | int | None
_RFC3339 = re.compile(r'^(\d{4}-\d\d-\d\d[Tt]\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?([Zz]|[+-]\d\d:\d\d)$')


def epoch_ns(value: RawTime, *, numeric_milliseconds: bool = False) -> int | None:
    """Parse timezone-bearing RFC3339 or explicitly typed integer milliseconds.

    Unknown/invalid timestamps remain unknown; original values are stored separately.
    Integer arithmetic keeps nanosecond distinctions within one second.
    """
    if isinstance(value, bool):
        return None
    if numeric_milliseconds and isinstance(value, (str, int)):
        number = str(value)
        if number.isascii() and number.isdecimal():
            return int(number) * 1_000_000
    if not isinstance(value, str):
        return None
    match = _RFC3339.fullmatch(value)
    if not match:
        return None
    base, fraction, offset = match.groups()
    try:
        moment = datetime.fromisoformat(base.upper() + ('+00:00' if offset.upper() == 'Z' else offset))
        delta = moment.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    except (ValueError, OverflowError):
        return None
    return (delta.days * 86400 + delta.seconds) * 1_000_000_000 + int((fraction or '').ljust(9, '0'))


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class Caption:
    meeting_id: str
    sentence_key: str
    text: str
    received_at: str
    mode: IngestMode = IngestMode.LIVE
    sentence_id: str | None = None
    speaker_id: str | None = None
    speaker_name: str | None = None
    language: str | None = None
    event_id: str | None = None
    event_time: RawTime = None
    start_time: RawTime = None
    end_time: RawTime = None
    # Only populated by an adapter with a verified, monotonic upstream revision contract.
    source_revision: int | None = None
    identity_degraded: bool = False

    @property
    def fingerprint(self) -> str:
        return digest([self.text, self.speaker_id, self.speaker_name, self.language, self.start_time, self.end_time])

    @property
    def event_ns(self) -> int | None:
        return epoch_ns(self.event_time)

    @property
    def end_ns(self) -> int | None:
        return epoch_ns(self.end_time, numeric_milliseconds=True)


@dataclass(frozen=True)
class Status:
    meeting_id: str
    state: SourceState
    reason: str  # Fixed application enum, never an arbitrary server message.
    at: str
    gap_start: int | None = None  # Unix seconds; suspected interval, not measured loss.
    gap_end: int | None = None


@dataclass(frozen=True)
class Delivery:
    sequence: int
    delivery_id: str
    kind: ChangeKind
    caption: Caption | None = None
    status: Status | None = None
    replayed: bool = False

    @property
    def historical(self) -> bool:
        return self.replayed or (self.caption is not None and self.caption.mode != IngestMode.LIVE)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> 'Delivery':
        caption = value.get('caption')
        status = value.get('status')
        return cls(sequence=value['sequence'], delivery_id=value['delivery_id'], kind=ChangeKind(value['kind']),
                   caption=Caption(**(caption | {'mode': IngestMode(caption['mode'])})) if caption else None,
                   status=Status(**(status | {'state': SourceState(status['state'])})) if status else None,
                   replayed=value.get('replayed', False))


class MeetingTranscriptSource(Protocol):
    """Durable reads never imply acknowledgement. Consumers persist before ack."""
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def read(self, consumer_id: str, *, limit: int = 100) -> Sequence[Delivery]: ...
    def acknowledge(self, consumer_id: str, sequence: int) -> None: ...
    def state(self) -> Status: ...


class FakeConsumer:
    """Contract example only. A durable variant is added with the storage layer."""
    def __init__(self):
        self.seen: set[str] = set()
        self.context: dict[tuple[str, str], Caption] = {}
        self.conflicts: list[Delivery] = []
        self.statuses: list[Status] = []
        self.fresh_inputs: list[str] = []

    def accept(self, delivery: Delivery) -> bool:
        if delivery.delivery_id in self.seen:
            return False
        self.seen.add(delivery.delivery_id)
        if delivery.status:
            self.statuses.append(delivery.status)
        if delivery.kind == ChangeKind.CONFLICT:
            self.conflicts.append(delivery)
        elif delivery.caption:
            caption = delivery.caption
            self.context[(caption.meeting_id, caption.sentence_key)] = caption
            if not delivery.historical and delivery.kind == ChangeKind.ADDED:
                self.fresh_inputs.append(delivery.delivery_id)
        return True
