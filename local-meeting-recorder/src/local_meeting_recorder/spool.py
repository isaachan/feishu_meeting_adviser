"""Durable spool writer per contract section 3.

Segments are append-only JSONL files named by their first sequence. Writes go
to a ``.tmp`` file first and are renamed atomically, so the advisor never
observes partial segments. Segments covered by the ack cursor may be removed.
"""
import json
import os
from pathlib import Path
import time


class SpoolWriter:
    def __init__(self, data_root: Path, session_id: str, stream_id: str, *,
                 roll_events: int = 200, roll_seconds: float = 2.0,
                 clock=time.time):
        recorder_root = Path(data_root) / 'recorder'
        self.stream_dir = recorder_root / 'spool' / session_id / stream_id
        self.ack_path = recorder_root / 'ack' / session_id / (stream_id + '.json')
        self.roll_events = roll_events
        self.roll_seconds = roll_seconds
        self.clock = clock
        self.stream_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._tmp = self.stream_dir / 'current.tmp'
        self._handle = None
        self._first_sequence = None
        self._events_in_segment = 0
        self._segment_opened_at = clock()

    def _open(self, event):
        self.stream_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._handle = self._tmp.open('a', encoding='utf-8')
        self._first_sequence = event['sequence']
        self._events_in_segment = 0
        self._segment_opened_at = self.clock()

    def _roll(self):
        if self._handle is None:
            return
        self._handle.flush()
        os.fsync(self._handle.fileno())
        self._handle.close()
        final = self.stream_dir / f'{self._first_sequence:06d}.jsonl'
        os.replace(self._tmp, final)
        self._handle = None
        self._first_sequence = None

    def append(self, event: dict) -> None:
        if self._handle is None:
            self._open(event)
        handle = self._handle
        if handle is None:
            raise RuntimeError('spool segment is not open')
        handle.write(json.dumps(event, ensure_ascii=False) + '\n')
        self._events_in_segment += 1
        if (self._events_in_segment >= self.roll_events
                or self.clock() - self._segment_opened_at >= self.roll_seconds):
            self._roll()

    def finish(self) -> None:
        self._roll()

    def acked_sequence(self) -> int:
        try:
            raw = self.ack_path.read_text(encoding='utf-8')
        except FileNotFoundError:
            return 0
        try:
            value = json.loads(raw)
        except ValueError:
            return 0
        return value.get('acked_sequence', 0) if isinstance(value, dict) else 0

    def cleanup(self) -> int:
        """Remove fully acknowledged segments; unacknowledged data always stays."""
        removed = 0
        acked = self.acked_sequence()
        for segment in sorted(self.stream_dir.glob('*.jsonl')):
            last = _last_sequence(segment)
            if last is not None and last <= acked:
                segment.unlink()
                removed += 1
        return removed


def _last_sequence(segment: Path) -> int | None:
    last = None
    try:
        with segment.open('r', encoding='utf-8') as stream:
            for line in stream:
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except ValueError:
                    return None
                sequence = value.get('sequence') if isinstance(value, dict) else None
                if not isinstance(sequence, int):
                    return None
                last = sequence
    except OSError:
        return None
    return last
