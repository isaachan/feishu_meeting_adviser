"""Domain enums, digest and timestamp parsing (ported from legacy advisor models.py).

Trimmed for plan C: no Feishu meeting channel, no pause participation, no spool
conflict/status deliveries. Captions arrive as immutable STT finals via ingest.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import StrEnum
import hashlib
import json
import re

RawTime = str | int | None
_RFC3339 = re.compile(r'^(\d{4}-\d\d-\d\d[Tt]\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?([Zz]|[+-]\d\d:\d\d)$')


class JobState(StrEnum):
    PENDING = 'pending'
    RUNNING = 'running'
    COMPLETE = 'complete'
    ABANDONED = 'abandoned'


class ReplyState(StrEnum):
    PENDING = 'pending'
    SENDING = 'sending'
    SENT = 'sent'
    UNCERTAIN = 'uncertain'
    REJECTED = 'rejected'
    CANCELLED = 'cancelled'


class MaterialState(StrEnum):
    DISABLED = 'disabled'
    READY = 'ready'
    PARTIAL = 'partial'


class SourceState(StrEnum):
    LIVE = 'live'
    DRAINED = 'drained'
    LOST = 'lost'


def epoch_ns(value: RawTime, *, numeric_milliseconds: bool = False) -> int | None:
    """Parse timezone-bearing RFC3339 or explicitly typed integer milliseconds."""
    if isinstance(value, bool):
        return None
    if numeric_milliseconds and isinstance(value, (str, int)):
        number = str(value)
        if len(number) <= 20 and number.isascii() and number.isdecimal():
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
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                      ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class Caption:
    meeting_id: str
    sentence_key: str
    text: str
    received_at: str
    sentence_id: str | None = None
    speaker_id: str | None = None
    speaker_name: str | None = None
    language: str | None = None
    event_id: str | None = None
    event_time: RawTime = None
    start_time: RawTime = None
    end_time: RawTime = None

    @property
    def fingerprint(self) -> str:
        return digest([self.text, self.speaker_id, self.speaker_name, self.language,
                       self.start_time, self.end_time])

    @property
    def event_ns(self) -> int | None:
        return epoch_ns(self.event_time)

    @property
    def end_ns(self) -> int | None:
        return epoch_ns(self.end_time, numeric_milliseconds=True)


def caption_from_json(raw) -> Caption:
    value = json.loads(raw)
    return Caption(**{key: item for key, item in value.items() if key in Caption.__dataclass_fields__})


def caption_to_dict(caption: Caption) -> dict:
    return asdict(caption)
