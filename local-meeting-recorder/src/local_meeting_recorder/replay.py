"""Timed fake replay: a script becomes contract events on the session timeline."""
import threading
from datetime import datetime, timezone
from pathlib import Path
import time
from uuid import uuid4

from .events import caption_event, stream_end_event
from .script import ScriptLine
from .spool import SpoolWriter


class FakeReplay:
    """Replay script lines at their delays; the replay start is the time origin."""

    def __init__(self, lines: list[ScriptLine], *, session_id: str, data_root: Path,
                 speed: float = 1.0, clock=time.time, sleep=None):
        if speed <= 0:
            raise ValueError('speed must be positive')
        self.lines = lines
        self.session_id = session_id
        self.stream_id = f'{session_id}:{uuid4().hex}'
        self.data_root = Path(data_root)
        self.speed = speed
        self.clock = clock
        self._sleep = sleep or time.sleep

    def run(self, stop_event: threading.Event | None = None) -> dict:
        stop_event = stop_event or threading.Event()
        writer = SpoolWriter(self.data_root, self.session_id, self.stream_id,
                             clock=self.clock)
        # utterance ids must stay unique across streams of one session
        nonce = self.stream_id.rsplit(':', 1)[-1][:8]
        sequence = 0
        offset_ms = 0
        reason = 'completed'
        try:
            for line in self.lines:
                if stop_event.is_set():
                    reason = 'stopped'
                    break
                if line.delay_ms:
                    # Interruptible wait scaled by the replay speed.
                    steps = max(1, int(line.delay_ms / self.speed / 50) or 1)
                    for _ in range(steps):
                        if stop_event.wait(line.delay_ms / self.speed / 1000 / steps):
                            break
                    if stop_event.is_set():
                        reason = 'stopped'
                        break
                sequence += 1
                offset_ms += line.delay_ms
                duration_ms = max(1, min(len(line.text), 5_000))
                writer.append(caption_event(
                    self.session_id, self.stream_id, sequence,
                    f'u-{nonce}-{sequence:06d}', line.text, line.speaker,
                    offset_ms, offset_ms + duration_ms,
                    datetime.now(timezone.utc).isoformat()))
        finally:
            if reason == 'completed':
                sequence += 1
                writer.append(stream_end_event(
                    self.session_id, self.stream_id, sequence, sequence - 1,
                    datetime.now(timezone.utc).isoformat()))
            writer.finish()
        return {'session_id': self.session_id, 'stream_id': self.stream_id,
                'events': sequence, 'stopped_reason': reason}
