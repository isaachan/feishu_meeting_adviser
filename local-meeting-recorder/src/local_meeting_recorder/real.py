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
import os
from pathlib import Path
import queue
import threading
import time
from typing import Callable
from uuid import uuid4

from .audio import CaptureSession
from .audio.frames import AudioFrame, SourceEvent
from .audio.sck_capture import SwiftLoopbackSource
from .audio.sources import AudioSource, MicSource
from .events import caption_event, stream_end_event
from .spool import SpoolWriter
from .stt import FINAL, StreamingSession, SttEvent, Transcriber, TranscriberError, VadSegmenter
from .stt.segmenter import Segment

MIC_SOURCE = 'mic'
LOOPBACK_SOURCE = 'loopback'
_DEFAULT_MIC_SPEAKER = '本机'


def _default_stt_model_dir() -> Path | None:
    env = os.environ.get('LMR_STT_MODEL_DIR')
    return Path(env) if env else None


class RealRecorder:
    """Runs one real capture stream end to end; stop via ``stop_event``.

    Clean stop: ``duration_s`` expires (stream_end is written). Interrupt:
    ``stop_event`` gets set by someone else (e.g. SIGINT) — no stream_end,
    unconfirmed audio stays in the spool for re-delivery.
    """

    def __init__(self, *, session_id: str, data_root: Path,
                 mic_enabled: bool = False,
                 mic_speaker: str = _DEFAULT_MIC_SPEAKER,
                 stt_mode: str = 'sherpa',
                 stt_model_dir: Path | None = None,
                 transcriber: Transcriber | None = None,
                 streaming_sessions: 'Callable[[str], StreamingSession] | None' = None,
                 sources: list[AudioSource] | None = None,
                 clock=time.time, monotonic=time.monotonic):
        self.session_id = session_id
        self.stream_id = f'{session_id}:{uuid4().hex}'
        self.data_root = Path(data_root)
        self.mic_enabled = mic_enabled
        self.mic_speaker = mic_speaker
        self.stt_mode = stt_mode
        self.stt_model_dir = Path(stt_model_dir) if stt_model_dir else None
        self._transcriber = transcriber
        self._streaming_sessions = streaming_sessions
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
        streaming = None
        if self.stt_mode == 'sherpa':
            make_session = self._streaming_sessions
            if make_session is None:
                from .stt import LocalSherpaSession
                model_dir = self.stt_model_dir or _default_stt_model_dir()
                if model_dir is None:
                    raise ValueError(
                        'sherpa STT needs --stt-model-dir or '
                        'LMR_STT_MODEL_DIR (see recorder README)')

                def default_factory(source_id: str) -> StreamingSession:
                    return LocalSherpaSession(source_id, model_dir=model_dir)

                make_session = default_factory
            streaming = {source.source_id: make_session(source.source_id)
                         for source in sources}
            for sess in streaming.values():
                sess.start()
        elif self.stt_mode == 'zhipu':
            if transcriber is None:
                from .stt import create_transcriber
                transcriber = create_transcriber({'stt': 'remote'})
        else:
            raise ValueError(f'unknown stt mode: {self.stt_mode}')

        audio_dir = self.data_root / 'recorder' / 'audio' / self.stream_id
        session = CaptureSession(sources, audio_dir,
                                 clock=self._clock, monotonic=self._monotonic)
        subscriber = session.subscribe()
        writer = SpoolWriter(self.data_root, self.session_id, self.stream_id,
                             clock=self._clock)
        segmenters = {source.source_id: VadSegmenter(source.source_id)
                      for source in sources}  # unused in streaming mode
        segment_queue: queue.Queue = queue.Queue()
        source_anchors: dict[str, float] = {}
        session_start = self._monotonic()
        last_text: dict[str, str] = {}
        counters = {'captions': 0, 'segments': 0, 'stt_failures': 0}
        errors: list[dict] = []
        latencies: list[dict] = []
        stopped_sources: set[str] = set()
        final_seq: dict[str, int] = {source.source_id: 0 for source in sources}

        def emit_streaming_final(source_id: str, event: SttEvent) -> None:
            if not event.text.strip():
                return
            sessions = streaming
            assert sessions is not None  # only called in streaming mode
            sess = sessions[source_id]
            anchor = source_anchors.get(source_id)
            if anchor is None:
                anchor = session_start
            start_ms = max(0, int((anchor + event.start_offset / 16000
                                   - session_start) * 1000))
            end_ms = max(start_ms + 1, int(
                (anchor + event.end_offset / 16000 - session_start) * 1000))
            caption_mono = self._monotonic()
            speech_end_mono = anchor + event.end_offset / 16000
            latencies.append({
                'source': source_id,
                'segment': event.start_offset,
                'after_segment_end_ms': int(max(
                    0.0, caption_mono - speech_end_mono) * 1000),
                'stt_ms': int(getattr(sess, 'decode_ms', 0.0)),
            })
            counters['segments'] += 1
            final_seq[source_id] += 1
            counters['captions'] += 1
            nonce = self.stream_id.rsplit(':', 1)[-1][:8]
            writer.append(caption_event(
                self.session_id, self.stream_id, counters['captions'],
                f'u-{nonce}-{source_id}-{final_seq[source_id]:06d}', event.text,
                speaker.get(source_id), start_ms, end_ms))

        def ingest() -> None:
            while len(stopped_sources) < len(sources):
                item = subscriber.get()
                if isinstance(item, AudioFrame):
                    anchor = source_anchors.get(item.source_id)
                    if anchor is None:
                        anchor = (item.captured_monotonic
                                  - item.sample_offset / item.rate)
                        source_anchors[item.source_id] = anchor
                    if streaming is not None:
                        for event in streaming[item.source_id].feed(item):
                            if event.status == FINAL:
                                emit_streaming_final(item.source_id, event)
                    else:
                        for seg in segmenters[item.source_id].feed(item):
                            counters['segments'] += 1
                            segment_queue.put(seg)
                elif isinstance(item, SourceEvent):
                    if item.type in ('gap_begin', 'source_paused'):
                        if streaming is not None:
                            for event in streaming[item.source_id].feed_gap():
                                if event.status == FINAL:
                                    emit_streaming_final(item.source_id, event)
                        else:
                            for seg in segmenters[item.source_id].feed_gap():
                                counters['segments'] += 1
                                segment_queue.put(seg)
                    elif item.type == 'source_stopped':
                        if streaming is not None:
                            for event in streaming[item.source_id].close():
                                if event.status == FINAL:
                                    emit_streaming_final(item.source_id, event)
                        else:
                            for seg in segmenters[item.source_id].flush():
                                counters['segments'] += 1
                                segment_queue.put(seg)
                        stopped_sources.add(item.source_id)
                    elif item.type == 'device_error':
                        errors.append({'source': item.source_id,
                                       'type': 'device_error',
                                       'message': item.detail.get('message')})

        ingest_thread = threading.Thread(target=ingest, daemon=True,
                                         name='real-ingest')
        stt_thread = None
        if not streaming:
            stt_latency_ctx = {'source_anchors': source_anchors,
                               'session_start': session_start,
                               'monotonic': self._monotonic}
            stt_thread = threading.Thread(
                target=self._transcribe_loop, kwargs=dict(
                    transcriber=transcriber, segment_queue=segment_queue,
                    writer=writer, speaker=speaker,
                    source_anchors=source_anchors,
                    session_start=session_start, last_text=last_text,
                    counters=counters, errors=errors, latencies=latencies,
                    latency_ctx=stt_latency_ctx),
                daemon=True, name='real-stt')

        session.start()
        ingest_thread.start()
        if stt_thread is not None:
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
        if stt_thread is not None:
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
            'stt_mode': self.stt_mode,
            'segments': counters['segments'],
            'captions': counters['captions'],
            'stt_failures': counters['stt_failures'],
            'errors': errors,
            'latencies': latencies,
        }
        return {'session_id': self.session_id, 'stream_id': self.stream_id,
                'events': counters['captions'],
                'stopped_reason': 'completed' if clean else 'stopped',
                **self._diagnostics}

    def _transcribe_loop(self, *, transcriber, segment_queue, writer, speaker,
                         source_anchors, session_start, last_text, counters,
                         errors, latencies, latency_ctx) -> None:
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
                speech_end_mono = anchor + segment.end_offset / 16000
                caption_mono = latency_ctx['monotonic']()
                latencies.append({
                    'source': segment.source_id,
                    'segment': segment.index,
                    'after_segment_end_ms': int(max(
                        0.0, caption_mono - speech_end_mono) * 1000),
                    'stt_ms': result.latency_ms,
                })
                counters['captions'] += 1
                nonce = self.stream_id.rsplit(':', 1)[-1][:8]
                writer.append(caption_event(
                    self.session_id, self.stream_id, counters['captions'],
                    f'u-{nonce}-{segment.source_id}-{segment.index:06d}', result.text,
                    speaker.get(segment.source_id), start_ms, end_ms))
