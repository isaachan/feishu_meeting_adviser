"""Spectator-mode contracts: immutable items, meeting references, time helpers."""
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from enum import StrEnum


class EventKind(StrEnum):
    TRANSCRIPT = 'transcript'
    CHAT = 'chat'
    PARTICIPANT_JOINED = 'participant_joined'
    PARTICIPANT_LEFT = 'participant_left'
    OTHER = 'other'


RawTime = str | int | None
_RFC3339 = re.compile(r'^(\d{4}-\d\d-\d\d[Tt]\d\d:\d\d:\d\d)(?:\.(\d{1,9}))?([Zz]|[+-]\d\d:\d\d)$')


def epoch_ms(value: RawTime) -> int | None:
    """Parse numeric milliseconds (int or decimal string) or RFC 3339 into epoch ms.

    Unknown or invalid timestamps stay unknown; callers never fabricate times.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (str, int)):
        number = str(value)
        if len(number) <= 20 and number.isascii() and number.isdecimal():
            return int(number)
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
    return (delta.days * 86400 + delta.seconds) * 1000 + int((fraction or '').ljust(3, '0')[:3])


def utc_now(clock) -> str:
    return datetime.fromtimestamp(clock(), timezone.utc).isoformat()


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class MeetingRef:
    meeting_id: str
    meeting_no: str = ''
    topic: str = ''

    def to_dict(self) -> dict:
        return {'meeting_id': self.meeting_id, 'meeting_no': self.meeting_no, 'topic': self.topic}


@dataclass(frozen=True)
class TranscriptItem:
    meeting_id: str
    item_key: str
    text: str
    received_at: str
    sentence_id: str | None = None
    speaker_id: str | None = None
    speaker_name: str | None = None
    language: str | None = None
    start_time: RawTime = None
    end_time: RawTime = None
    identity_degraded: bool = False

    @property
    def kind(self) -> EventKind:
        return EventKind.TRANSCRIPT

    def to_dict(self) -> dict:
        return {'kind': self.kind.value, 'meeting_id': self.meeting_id, 'item_key': self.item_key,
                'received_at': self.received_at, 'sentence_id': self.sentence_id,
                'speaker_id': self.speaker_id, 'speaker_name': self.speaker_name,
                'language': self.language, 'start_time': self.start_time,
                'end_time': self.end_time, 'identity_degraded': self.identity_degraded,
                'text': self.text}

    @property
    def end_ms(self) -> int | None:
        return epoch_ms(self.end_time)


@dataclass(frozen=True)
class ChatMessage:
    meeting_id: str
    item_key: str
    content: str
    received_at: str
    message_id: str | None = None
    sender_id: str | None = None
    sender_name: str | None = None
    message_type: int | None = None
    send_time: RawTime = None

    @property
    def kind(self) -> EventKind:
        return EventKind.CHAT

    def to_dict(self) -> dict:
        return {'kind': self.kind.value, 'meeting_id': self.meeting_id, 'item_key': self.item_key,
                'received_at': self.received_at, 'message_id': self.message_id,
                'sender_id': self.sender_id, 'sender_name': self.sender_name,
                'message_type': self.message_type, 'send_time': self.send_time,
                'content': self.content}


@dataclass(frozen=True)
class ParticipantChange:
    meeting_id: str
    item_key: str
    direction: str  # 'joined' or 'left'
    received_at: str
    participant_id: str | None = None
    participant_name: str | None = None
    at_time: RawTime = None
    leave_reason: int | None = None

    @property
    def kind(self) -> EventKind:
        return EventKind.PARTICIPANT_JOINED if self.direction == 'joined' else EventKind.PARTICIPANT_LEFT

    def to_dict(self) -> dict:
        return {'kind': self.kind.value, 'meeting_id': self.meeting_id, 'item_key': self.item_key,
                'received_at': self.received_at, 'participant_id': self.participant_id,
                'participant_name': self.participant_name, 'direction': self.direction,
                'at_time': self.at_time, 'leave_reason': self.leave_reason}


MeetingItem = TranscriptItem | ChatMessage | ParticipantChange
