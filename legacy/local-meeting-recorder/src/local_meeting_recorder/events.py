"""Contract event builders for the recorder input contract (schema v1).

Field semantics are frozen in docs/CONTRACT_RECORDER_INPUT.md; changes must
bump schema_version and update both modules plus the integration tests.
"""
from datetime import datetime, timezone

SCHEMA_VERSION = 1


def _wall_clock() -> str:
    return datetime.now(timezone.utc).isoformat()


def event_id(stream_id: str, sequence: int) -> str:
    return f'{stream_id}:{sequence}'


def caption_event(session_id: str, stream_id: str, sequence: int, utterance_id: str,
                  text: str, speaker_label: str | None, start_offset_ms: int,
                  end_offset_ms: int, wall_clock_at: str | None = None) -> dict:
    if end_offset_ms < start_offset_ms or start_offset_ms < 0:
        raise ValueError('caption offsets must satisfy end >= start >= 0')
    return {
        'schema_version': SCHEMA_VERSION,
        'session_id': session_id,
        'stream_id': stream_id,
        'sequence': sequence,
        'event_id': event_id(stream_id, sequence),
        'kind': 'caption',
        'utterance_id': utterance_id,
        'revision': 1,
        'text': text,
        'speaker_label': speaker_label,
        'start_offset_ms': start_offset_ms,
        'end_offset_ms': end_offset_ms,
        'wall_clock_at': wall_clock_at or _wall_clock(),
    }


def stream_end_event(session_id: str, stream_id: str, sequence: int, last_sequence: int,
                     wall_clock_at: str | None = None) -> dict:
    return {
        'schema_version': SCHEMA_VERSION,
        'session_id': session_id,
        'stream_id': stream_id,
        'sequence': sequence,
        'event_id': event_id(stream_id, sequence),
        'kind': 'stream_end',
        'last_sequence': last_sequence,
        'wall_clock_at': wall_clock_at or _wall_clock(),
    }
