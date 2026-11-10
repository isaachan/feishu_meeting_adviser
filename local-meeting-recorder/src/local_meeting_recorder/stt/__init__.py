"""STT layer (stage 5.4). Interface frozen 2026-11-09.

Contract:
- Input: AudioFrame items per source (16 kHz / mono / float32, cumulative
  sample_offset timeline; pauses arrive as SourceEvent gap markers).
- VadSegmenter turns frames into Segments bounded by voice activity
  (max_segment_ms keeps every segment inside the GLM-ASR 30 s file limit).
- Transcriber.transcribe(pcm, rate=..., prompt=..., request_id=...) returns a
  TranscriptionResult with FINAL text only; partials are not produced and do
  not trigger any model decision in this stage.
- Speaker routing stays out of STT: segments carry their source_id and the
  contract writer (stage 5.5) maps mic -> local user, loopback -> remote or
  unknown.
- Configuration switch ``stt``: ``sherpa`` (default since 2026-11-10; local
  streaming via sherpa-onnx), ``zhipu`` (optional file-upload provider; the
  GLM Coding Plan carries no ASR quota, error 1113) and later
  ``remote-stream`` (server-side sessions, task 5.11).
- Streaming extension (2026-11-10): StreamingSession.feed(frame) returns
  partial/final SttEvent items carrying source-timeline offsets; partials
  never reach the caption contract; gaps close utterances explicitly.
"""
from .base import Transcriber, TranscriptionResult, TranscriberError
from .remote import RemoteStreamError, RemoteStreamSession
from .segmenter import Segment, VadSegmenter
from .streaming import FINAL, PARTIAL, LocalSherpaSession, SttEvent, StreamingSession
from .zhipu import ZhipuASR

def create_transcriber(config: dict) -> Transcriber:
    """Build the configured transcriber (``stt: remote|local``)."""
    kind = config.get('stt', 'remote')
    if kind == 'remote':
        provider = config.get('provider', 'zhipu')
        if provider != 'zhipu':
            raise TranscriberError(f'unknown remote stt provider: {provider}')
        return ZhipuASR(api_key=config.get('api_key'),
                        base_url=config.get('base_url'),
                        model=config.get('model'))
    if kind == 'local':
        raise TranscriberError(
            'local stt is an optional extra and not implemented yet')
    raise TranscriberError(f'unknown stt mode: {kind}')


__all__ = [
    'FINAL',
    'PARTIAL',
    'RemoteStreamError',
    'RemoteStreamSession',
    'Segment',
    'LocalSherpaSession',
    'StreamingSession',
    'SttEvent',
    'Transcriber',
    'TranscriberError',
    'TranscriptionResult',
    'VadSegmenter',
    'ZhipuASR',
    'create_transcriber',
]
