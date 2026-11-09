"""CaptureSession: aggregate dual sources into retained audio + events.

Per source a writer thread drains the producer's queue and:
  1. appends PCM to ``<out_dir>/<source_id>.wav`` (whole-session retention,
     16-bit PCM encoding of the float32 stream), and
  2. fans the item out to every subscriber queue (STT in stage 5.4).

Producers only ever append to their deque, so a slow STT subscriber cannot
block capture; subscribers get their own unbounded queue and must drain it
themselves. Timeline: frames carry a cumulative ``sample_offset`` per
source; pauses are explicit gap events, never folded into the sample count.
"""
from collections import deque
import json
import queue
import threading
import time
import wave
from pathlib import Path

from .frames import AudioFrame, SourceEvent
from .sources import AudioSource

_WRITER_POLL_S = 0.05


def _to_int16_pcm(data: bytes) -> bytes:
    import array

    floats = array.array('f')
    floats.frombytes(data)
    out = array.array('h', (int(max(-1.0, min(1.0, v)) * 32766) for v in floats))
    return out.tobytes()


class CaptureSession:
    """Own wav retention, the events log and subscriber fan-out."""

    def __init__(self, sources: list[AudioSource], out_dir: Path, *,
                 clock=time.time, monotonic=time.monotonic):
        if not sources:
            raise ValueError('at least one source is required')
        self.sources = {source.source_id: source for source in sources}
        if len(self.sources) != len(sources):
            raise ValueError('source ids must be unique')
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._monotonic = monotonic
        self._pending: dict[str, deque] = {sid: deque() for sid in self.sources}
        self._stopped: dict[str, threading.Event] = {sid: threading.Event() for sid in self.sources}
        self._writers: dict[str, threading.Thread] = {}
        self._wavs: dict[str, wave.Wave_write] = {}
        self._events_lock = threading.Lock()
        self._subscribers: list[queue.Queue] = []
        self._subscribers_lock = threading.Lock()
        self._stats: dict[str, dict] = {
            sid: {'frames': 0, 'bytes': 0, 'end_offset': 0, 'gaps': 0,
                  'warnings': 0}
            for sid in self.sources
        }
        self._started = False
        self._finished = False

    def subscribe(self) -> queue.Queue:
        """Get a queue receiving every AudioFrame/SourceEvent from now on."""
        with self._subscribers_lock:
            subscriber: queue.Queue = queue.Queue()
            self._subscribers.append(subscriber)
            return subscriber

    def start(self) -> None:
        if self._started:
            raise RuntimeError('session already started')
        self._started = True
        self._log_event({'type': 'session_started',
                         'wall': self._now_iso(),
                         'sources': sorted(self.sources)})
        for sid, source in self.sources.items():
            wav_path = self.out_dir / f'{sid}.wav'
            wav = wave.open(str(wav_path), 'wb')
            wav.setnchannels(source.channels)
            wav.setsampwidth(2)
            wav.setframerate(source.rate)
            self._wavs[sid] = wav
            self._log_event({'type': 'wav_opened', 'source': sid,
                             'path': str(wav_path),
                             'rate': source.rate, 'channels': source.channels})
            writer = threading.Thread(target=self._write_loop, args=(sid,),
                                      daemon=True, name=f'writer-{sid}')
            self._writers[sid] = writer
            writer.start()
            source.start(self._make_sink(sid))

    def pause(self, source_id: str) -> None:
        self.sources[source_id].pause()

    def resume(self, source_id: str) -> None:
        self.sources[source_id].resume()

    def stop(self, timeout: float = 15.0) -> dict:
        """Stop sources, drain writers, close files; returns the summary."""
        if self._finished:
            return self._summary()
        for source in self.sources.values():
            source.stop()
        for sid in self.sources:
            self._stopped[sid].set()
        deadline = time.monotonic() + timeout
        for writer in self._writers.values():
            writer.join(timeout=max(0.1, deadline - time.monotonic()))
        for sid, wav in self._wavs.items():
            try:
                wav.close()
            except Exception:  # noqa: BLE001 - close best-effort
                pass
        self._finished = True
        summary = self._summary()
        self._log_event({'type': 'session_stopped', 'wall': self._now_iso(),
                         'summary': summary})
        return summary

    def _make_sink(self, sid: str):
        pending = self._pending[sid]

        def sink(item: object) -> None:
            pending.append(item)

        return sink

    def _write_loop(self, sid: str) -> None:
        pending = self._pending[sid]
        stopped = self._stopped[sid]
        wav = self._wavs[sid]
        stats = self._stats[sid]
        while True:
            try:
                item = pending.popleft()
            except IndexError:
                if stopped.is_set():
                    return
                time.sleep(_WRITER_POLL_S)
                continue
            self._deliver(item)
            if isinstance(item, AudioFrame):
                wav.writeframes(_to_int16_pcm(item.data))
                stats['frames'] += item.frames
                stats['bytes'] += len(item.data)
                stats['end_offset'] = item.end_offset
            elif isinstance(item, SourceEvent):
                record = {'type': item.type, 'source': sid,
                          'mono': item.captured_monotonic, **item.detail}
                self._log_event(record)
                if item.type == 'gap_begin':
                    stats['gaps'] += 1
                if item.type in ('device_error', 'device_warning'):
                    stats['warnings'] += 1

    def _deliver(self, item: object) -> None:
        with self._subscribers_lock:
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            subscriber.put_nowait(item)

    def _log_event(self, record: dict) -> None:
        line = json.dumps(record, ensure_ascii=False, default=str)
        with self._events_lock:
            with (self.out_dir / 'events.jsonl').open('a', encoding='utf-8') as fh:
                fh.write(line + '\n')

    def _now_iso(self) -> str:
        from datetime import datetime, timezone

        return datetime.fromtimestamp(self._clock(), timezone.utc).isoformat()

    def _summary(self) -> dict:
        result = {'out_dir': str(self.out_dir), 'sources': {}}
        for sid, source in self.sources.items():
            stats = self._stats[sid]
            result['sources'][sid] = {
                'rate': source.rate,
                'channels': source.channels,
                'frames': stats['frames'],
                'duration_s': round(stats['end_offset'] / source.rate, 3)
                if source.rate else 0,
                'gaps': stats['gaps'],
                'warnings': stats['warnings'],
            }
        return result
