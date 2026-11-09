"""Real capture pipeline: dual sources -> VAD -> STT -> contract spool.

Same recorder input contract as fake replay (schema v1): captions carry the
stream-relative offsets of the spoken audio; ``stream_end`` is only written
on a clean stop (duration expiry), never fabricated after an interrupt
(external stop event, e.g. Ctrl-C). Speaker routing: the mic source is
labelled as the local user (configurable), the loopback source keeps
``None`` (remote/unknown) — the recording user is never assumed to be every
speaker.

Whole-session audio retention lands next to the spool under
``<data_root>/recorder/audio/<stream_id>/`` and is kept until manually
deleted (2026-11-09 decision).
"""
from datetime import datetime, timezone
from pathlib import Path
import queue
import threading
import time
from uuid import uuid4

from .audio import CaptureSession
from .audio.frames import AudioFrame, SourceEvent
from .audio.sck_capture import SwiftLoopbackSource
from .audio.sources import AudioSource, MicSource
from .events import caption_event, stream_end_event
from .spool import SpoolWriter
from .stt import Transcriber, TranscriberError, VadSegmenter
from .stt.segmenter import Segment

MIC_SOURCE = 'mic'
LOOPBACK_SOURCE = 'loopback'
_DEFAULT_MIC_SPEAKER = '本机'


class RealRecorder:
    """Runs one real capture stream end to end; stop via ``stop_event``.

    Clean stop: ``duration_s`` expires (stream_end is written). Interrupt:
    ``stop_event`` gets set by someone else (e.g. SIGINT) — no stream_end,
    unconfirmed audio stays in the spool for re-delivery.
    """

    def __init__(self, *, session_id: str, data_root: Path,
                 mic_enabled: bool = False,
                 mic_speaker: str = _DEFAULT_MIC_SPEAKER,
                 transcriber: Transcriber | None = None,
                 sources: list[AudioSource] | None = None,
                 clock=time.time, monotonic=time.monotonic):
        self.session_id = session_id
        self.stream_id = f'{session_id}:{uuid4().hex}'
        self.data_root = Path(data_root)
        self.mic_enabled = mic_enabled
        self.mic_speaker = mic_speaker
        self._transcriber = transcriber
        self._source_override = sources
        self._clock = clock
        self._monotonic = monotonic
        self._diagnostics: dict = {}

    @property
    def diagnostics(self) -> dict:
        return self._diagnostics

    def run(self, stop_event: threading.Event | None = None,
            duration_s: float | None = None) -> dict:
        stop_event = stop_event or threading.Event()
        if self._source_override is not None:
            sources: list[AudioSource] = list(self._source_override)
        else:
            sources = [SwiftLoopbackSource(LOOPBACK_SOURCE)]
            if self.mic_enabled:
                sources.append(MicSource(MIC_SOURCE))
        speaker = {MIC_SOURCE: self.mic_speaker if self.mic_enabled else None,
                   LOOPBACK_SOURCE: None}
        transcriber = self._transcriber
        if transcriber is None:
            from .stt import create_transcriber
            transcriber = create_transcriber({})

        audio_dir = self.data_root / 'recorder' / 'audio' / self.stream_id
        session = CaptureSession(sources, audio_dir,
                                 clock=self._clock, monotonic=self._monotonic)
        subscriber = session.subscribe()
        writer = SpoolWriter(self.data_root, self.session_id, self.stream_id,
                             clock=self._clock)
        segmenters = {source.source_id: VadSegmenter(source.source_id)
                      for source in sources}
        segment_queue: queue.Queue[Segment | None] = queue.Queue()
        source_anchors: dict[str, float] = {}
        session_start = self._monotonic()
        last_text: dict[str, str] = {}
        counters = {'captions': 0, 'segments': 0, 'stt_failures': 0}
        errors: list[dict] = []
        stopped_sources: set[str] = set()

        def ingest() -> None:
            while len(stopped_sources) < len(sources):
                item = subscriber.get()
                if isinstance(item, AudioFrame):
                    anchor = source_anchors.get(item.source_id)
                    if anchor is None:
                        anchor = (item.captured_monotonic
                                  - item.sample_offset / item.rate)
                        source_anchors[item.source_id] = anchor
                    for segment in segmenters[item.source_id].feed(item):
                        counters['segments'] += 1
                        segment_queue.put(segment)
                elif isinstance(item, SourceEvent):
                    if item.type in ('gap_begin', 'source_paused'):
                        for segment in segmenters[item.source_id].feed_gap():
                            counters['segments'] += 1
                            segment_queue.put(segment)
                    elif item.type == 'source_stopped':
                        for segment in segmenters[item.source_id].flush():
                            counters['segments'] += 1
                            segment_queue.put(segment)
                        stopped_sources.add(item.source_id)
                    elif item.type == 'device_error':
                        errors.append({'source': item.source_id,
                                       'type': 'device_error',
                                       'message': item.detail.get('message')})

        ingest_thread = threading.Thread(target=ingest, daemon=True,
                                         name='real-ingest')
        stt_thread = threading.Thread(
            target=self._transcribe_loop, kwargs=dict(
                transcriber=transcriber, segment_queue=segment_queue,
                writer=writer, speaker=speaker, source_anchors=source_anchors,
                session_start=session_start, last_text=last_text,
                counters=counters, errors=errors),
            daemon=True, name='real-stt')

        session.start()
        ingest_thread.start()
        stt_thread.start()

        clean = False
        if duration_s is not None:
            clean = not stop_event.wait(duration_s)
            if clean:
                stop_event.set()
        else:
            stop_event.wait()

        session.stop()  # sources emit source_stopped -> ingest flushes & exits
        ingest_thread.join(timeout=10)
        segment_queue.put(None)
        stt_thread.join(timeout=30)
        if clean:
            sequence = counters['captions'] + 1
            writer.append(stream_end_event(
                self.session_id, self.stream_id, sequence,
                sequence - 1, datetime.now(timezone.utc).isoformat()))
            counters['captions'] = sequence
        writer.finish()
        writer.cleanup()
        self._diagnostics = {
            'stream_id': self.stream_id,
            'segments': counters['segments'],
            'captions': counters['captions'],
            'stt_failures': counters['stt_failures'],
            'errors': errors,
        }
        return {'session_id': self.session_id, 'stream_id': self.stream_id,
                'events': counters['captions'],
                'stopped_reason': 'completed' if clean else 'stopped',
                **self._diagnostics}

    def _transcribe_loop(self, *, transcriber, segment_queue, writer, speaker,
                         source_anchors, session_start, last_text, counters,
                         errors) -> None:
        while True:
            segment = segment_queue.get()
            if segment is None:
                return
            prompt = last_text.get(segment.source_id) or None
            # short unique id: the ASR API rejects ids outside 6..64 chars
            request_id = f'lmr-{segment.source_id[:4]}-{uuid4().hex[:20]}'
            try:
                result = transcriber.transcribe(
                    segment.pcm, rate=16000, prompt=prompt,
                    request_id=request_id)
            except TranscriberError as exc:
                counters['stt_failures'] += 1
                errors.append({'source': segment.source_id,
                               'segment': segment.index,
                               'reason': exc.reason, 'message': str(exc)})
                continue
            if result.text.strip():
                last_text[segment.source_id] = result.text
                anchor = source_anchors.get(segment.source_id, session_start)
                start_ms = max(0, int((anchor + segment.start_offset / 16000
                                       - session_start) * 1000))
                end_ms = max(start_ms + 1, int(
                    (anchor + segment.end_offset / 16000 - session_start)
                    * 1000))
                counters['captions'] += 1
                writer.append(caption_event(
                    self.session_id, self.stream_id, counters['captions'],
                    f'u-{segment.source_id}-{segment.index:06d}', result.text,
                    speaker.get(segment.source_id), start_ms, end_ms))
