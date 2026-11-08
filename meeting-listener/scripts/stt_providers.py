"""Streaming STT sessions. PCM input is 16 kHz, mono, signed 16-bit LE.

send_pcm() and finish() return zero or more normalized events. A partial can be
revised; only final events should be committed to meeting context.
"""
from pathlib import Path
from typing import Protocol

SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2


class STTSession(Protocol):
    chunks: int
    pcm_bytes: int

    def start(self) -> None: ...
    def send_pcm(self, pcm: bytes, sample_rate: int) -> list[dict]: ...
    def finish(self) -> list[dict]: ...
    def cancel(self) -> None: ...


def validate_pcm(pcm: bytes, sample_rate: int) -> None:
    if sample_rate != SAMPLE_RATE or not pcm or len(pcm) % BYTES_PER_SAMPLE:
        raise ValueError('Expected nonempty 16kHz s16le mono PCM samples')


class MockSTT:
    """Transport-only receipt; the text is NOT recognized speech."""
    def __init__(self):
        self.chunks = 0
        self.pcm_bytes = 0

    def start(self):
        pass

    def send_pcm(self, pcm, sample_rate):
        validate_pcm(pcm, sample_rate)
        self.chunks += 1
        self.pcm_bytes += len(pcm)
        return [{'type': 'mock_receipt', 'seq': self.chunks,
                 'text': f'[MOCK] received {self.pcm_bytes / (SAMPLE_RATE * BYTES_PER_SAMPLE):.3f}s PCM',
                 'duration_s': len(pcm) / (SAMPLE_RATE * BYTES_PER_SAMPLE)}]

    def finish(self):
        return [{'type': 'mock_done', 'chunks': self.chunks,
                 'audio_seconds': self.pcm_bytes / (SAMPLE_RATE * BYTES_PER_SAMPLE)}]

    def cancel(self):
        pass


class LocalSherpaSTT:
    """One stateful sherpa-onnx streaming transducer session (CPU on macOS).

    Model must contain tokens.txt, encoder.int8.onnx, decoder.onnx,
    joiner.int8.onnx. No per-chunk offline decoding or file upload.
    """
    def __init__(self, model_dir: Path, num_threads: int = 2):
        self.model_dir = Path(model_dir)
        self.num_threads = num_threads
        self.chunks = 0
        self.pcm_bytes = 0
        self.recognizer = None
        self.stream = None
        self.np = None
        self.segment = 1
        self.last_partial = ''

    def start(self):
        if self.num_threads < 1:
            raise ValueError('num_threads must be positive')
        files = ('tokens.txt', 'encoder.int8.onnx', 'decoder.onnx', 'joiner.int8.onnx')
        missing = [name for name in files if not (self.model_dir / name).is_file()]
        if missing:
            raise FileNotFoundError(f'Missing local STT model files in {self.model_dir}: {missing}')
        try:
            import numpy as np
            import sherpa_onnx
        except ImportError as exc:
            raise RuntimeError('Install local STT deps: python3 -m pip install -r requirements-local.txt') from exc
        self.np = np
        self.recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=str(self.model_dir / 'tokens.txt'),
            encoder=str(self.model_dir / 'encoder.int8.onnx'),
            decoder=str(self.model_dir / 'decoder.onnx'),
            joiner=str(self.model_dir / 'joiner.int8.onnx'),
            num_threads=self.num_threads, provider='cpu', sample_rate=SAMPLE_RATE,
            feature_dim=80, enable_endpoint_detection=True,
            rule1_min_trailing_silence=2.4, rule2_min_trailing_silence=1.2,
            rule3_min_utterance_length=300)
        self.stream = self.recognizer.create_stream()

    def _decode(self):
        while self.recognizer.is_ready(self.stream):
            self.recognizer.decode_stream(self.stream)

    def _events(self):
        text = self.recognizer.get_result(self.stream).strip()
        endpoint = self.recognizer.is_endpoint(self.stream)
        events = []
        if endpoint:
            if text:
                events.append({'type': 'transcript', 'status': 'final', 'text': text,
                               'segment': self.segment, 'audio_seconds': self.pcm_bytes / (SAMPLE_RATE * BYTES_PER_SAMPLE)})
            self.recognizer.reset(self.stream)
            self.segment += 1
            self.last_partial = ''
        elif text != self.last_partial:
            # Empty revisions must also clear a previously emitted partial.
            events.append({'type': 'transcript', 'status': 'partial', 'text': text,
                           'segment': self.segment, 'audio_seconds': self.pcm_bytes / (SAMPLE_RATE * BYTES_PER_SAMPLE)})
            self.last_partial = text
        return events

    def send_pcm(self, pcm, sample_rate):
        validate_pcm(pcm, sample_rate)
        if self.stream is None:
            raise RuntimeError('Local STT has not been started')
        self.chunks += 1
        self.pcm_bytes += len(pcm)
        samples = self.np.frombuffer(pcm, dtype='<i2').astype(self.np.float32) / 32768.0
        self.stream.accept_waveform(SAMPLE_RATE, samples)
        self._decode()
        return self._events()

    def finish(self):
        if self.stream is None:
            return []
        # Streaming recognizer needs padding to decode the last incomplete window.
        self.stream.accept_waveform(SAMPLE_RATE, self.np.zeros(int(.7*SAMPLE_RATE), dtype=self.np.float32))
        self.stream.input_finished()
        self._decode()
        text = self.recognizer.get_result(self.stream).strip()
        self.stream = None
        if text:
            return [{'type': 'transcript', 'status': 'final', 'text': text, 'segment': self.segment,
                     'audio_seconds': self.pcm_bytes / (SAMPLE_RATE * BYTES_PER_SAMPLE)}]
        return []

    def cancel(self):
        self.stream = None
        self.recognizer = None


def create_stt_session(provider: str, *, model_dir=None, local_threads=2) -> STTSession:
    if provider == 'mock':
        return MockSTT()
    if provider == 'local':
        if model_dir is None:
            raise ValueError('--local-model-dir is required for --stt-provider local')
        return LocalSherpaSTT(Path(model_dir), num_threads=local_threads)
    raise ValueError(f'Unknown STT provider: {provider}')
