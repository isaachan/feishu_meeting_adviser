"""Advisor side of the recorder handoff contract.

Implements docs/CONTRACT_RECORDER_INPUT.md: read spool segments in sequence
order, persist captions through the durable source store, and only then
advance the ack cursor. Re-read events are absorbed by the store's dedup;
a gap pauses its stream until the missing segment appears.
"""
import json
import os
from pathlib import Path

from .models import Caption, IngestMode, digest
from .storage import StorageError, Store

SCHEMA_VERSION = 1
MAX_TEXT_CHARACTERS = 2000


class SpoolIngester:
    def __init__(self, source: Store, data_root: Path, session_id: str, *,
                 started_at: float):
        recorder_root = Path(data_root) / 'recorder'
        self.source = source
        self.session_id = session_id
        self.spool_root = recorder_root / 'spool' / session_id
        self.ack_root = recorder_root / 'ack' / session_id
        self.started_at = started_at
        self._drained: set[str] = set()

    def _streams(self) -> list[Path]:
        if not self.spool_root.exists():
            return []
        return sorted(path for path in self.spool_root.iterdir() if path.is_dir())

    def _ack_path(self, stream: str) -> Path:
        return self.ack_root / (stream + '.json')

    def _acked(self, stream: str) -> int:
        try:
            raw = self._ack_path(stream).read_text(encoding='utf-8')
        except FileNotFoundError:
            return 0
        try:
            value = json.loads(raw)
        except ValueError:
            raise StorageError('recorder ack file is damaged') from None
        number = value.get('acked_sequence') if isinstance(value, dict) else None
        if not isinstance(number, int) or isinstance(number, bool) or number < 0:
            raise StorageError('recorder ack file is damaged')
        return number

    def _write_ack(self, stream: str, number: int) -> None:
        self.ack_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = self._ack_path(stream)
        tmp = path.with_name(path.name + '.tmp')
        tmp.write_text(json.dumps({'acked_sequence': number}), encoding='utf-8')
        os.replace(tmp, path)

    def _events_after(self, stream_dir: Path, acked: int) -> list[dict]:
        events: list[tuple[int, dict]] = []
        for segment in sorted(stream_dir.glob('*.jsonl')):
            for line in segment.read_text(encoding='utf-8').splitlines():
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    raise StorageError('recorder spool segment is damaged') from None
                sequence = event.get('sequence') if isinstance(event, dict) else None
                if not isinstance(sequence, int) or isinstance(sequence, bool):
                    raise StorageError('recorder spool event is damaged')
                if sequence > acked:
                    events.append((sequence, event))
        events.sort(key=lambda item: item[0])
        ordered: list[dict] = []
        expected = acked + 1
        for sequence, event in events:
            if sequence != expected:
                break  # A gap waits for its segment; never skip ahead.
            ordered.append(event)
            expected += 1
        return ordered

    def _validate(self, event: dict) -> None:
        if event.get('schema_version') != SCHEMA_VERSION:
            raise StorageError('recorder spool schema version is unsupported')
        if (event.get('session_id') != self.session_id
                or not isinstance(event.get('stream_id'), str) or not event['stream_id']):
            raise StorageError('recorder spool event does not belong to this session')

    def _caption(self, event: dict) -> Caption:
        text = event.get('text')
        if (not isinstance(text, str) or not text.strip()
                or len(text) > MAX_TEXT_CHARACTERS):
            raise StorageError('recorder spool caption text is invalid')
        utterance = event.get('utterance_id')
        if not isinstance(utterance, str) or not utterance:
            raise StorageError('recorder spool caption identity is invalid')
        start, end = event.get('start_offset_ms'), event.get('end_offset_ms')
        if (not isinstance(start, int) or isinstance(start, bool)
                or not isinstance(end, int) or isinstance(end, bool)
                or end < start or start < 0):
            raise StorageError('recorder spool caption offsets are invalid')
        speaker = event.get('speaker_label')
        if speaker is not None and (not isinstance(speaker, str) or not speaker.strip()):
            raise StorageError('recorder spool speaker label is invalid')
        wall = event.get('wall_clock_at')
        wall_clock = wall if isinstance(wall, str) and wall else ''
        started_ms = int(round(self.started_at * 1000))
        return Caption(
            self.session_id, utterance, text, wall_clock,
            mode=IngestMode.LIVE,
            speaker_id=('speaker-' + digest(speaker)[:16]) if speaker else None,
            speaker_name=speaker,
            event_id=event.get('event_id'),
            event_time=wall_clock or None,
            start_time=started_ms + start,
            end_time=started_ms + end,
        )

    def poll(self) -> int:
        """Ingest newly available events; returns captions delivered this pass."""
        added = 0
        for stream_dir in self._streams():
            stream = stream_dir.name
            acked = self._acked(stream)
            events = self._events_after(stream_dir, acked)
            if not events:
                continue
            captions: list[Caption] = []
            end_sequence = None
            processed = acked
            for event in events:
                self._validate(event)
                kind = event.get('kind')
                if kind == 'caption':
                    captions.append(self._caption(event))
                elif kind == 'stream_end':
                    last = event.get('last_sequence')
                    if not isinstance(last, int) or isinstance(last, bool) or last < 0:
                        raise StorageError('recorder stream_end is invalid')
                    end_sequence = last
                else:
                    raise StorageError('recorder spool event kind is unknown')
                processed = event['sequence']
            if captions:
                self.source.ensure_meeting(self.session_id, {})
                self.source.commit_page(self.session_id, captions, {})
                added += len(captions)
            if processed > acked:
                self._write_ack(stream, processed)
            if end_sequence is not None and processed >= end_sequence:
                self._drained.add(stream)
        return added

    def drained(self) -> bool:
        """True when every observed stream is consumed through its stream_end."""
        return all(stream.name in self._drained for stream in self._streams())
