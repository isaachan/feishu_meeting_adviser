"""Server-side sherpa sessions per CONTRACT_STT_STREAM (one per connection).

Two engines share this module and the SttEvent surface:

- ``ServerSession``: streaming zipformer transducer. Adapted from
  local-meeting-recorder's LocalSherpaSession on purpose (module
  independence): input is int16 PCM, offsets count received samples
  from this connection's origin, endpoint detection produces final
  events, explicit gaps close utterances at the boundary.
- ``FireRedSession``: non-streaming fire-red-asr2 AED model. silero VAD
  segments the stream (silence or max-speech cap closes a segment);
  each closed segment is decoded whole and emitted as one final.
  No partial events (advisor consumes finals only).
"""
from dataclasses import dataclass
from pathlib import Path
import time

RATE = 16000
PARTIAL = 'partial'
FINAL = 'final'

_MODEL_FILES = ('tokens.txt', 'encoder.int8.onnx', 'decoder.onnx',
                'joiner.int8.onnx')

FIRE_RED_FILES = ('tokens.txt', 'encoder.int8.onnx', 'decoder.int8.onnx',
                  'silero_vad.onnx')

VAD_THRESHOLD = 0.5
VAD_MIN_SILENCE_S = 0.5
VAD_MIN_SPEECH_S = 0.25
VAD_MAX_SPEECH_S = 15.0
VAD_BUFFER_S = 60


def fire_red_model(model_dir: Path) -> bool:
    """Detect a fire-red-asr AED model dir (no joiner, int8 decoder)."""
    model_dir = Path(model_dir)
    if (model_dir / 'joiner.int8.onnx').is_file():
        return False
    return ((model_dir / 'encoder.int8.onnx').is_file()
            and (model_dir / 'decoder.int8.onnx').is_file())


def _pcm_to_floats(pcm: bytes, np_module):
    if np_module is not None:
        return (np_module.frombuffer(pcm, dtype='<i2').astype(
            np_module.float32) / 32768.0)
    import array

    ints = array.array('h')
    ints.frombytes(pcm)
    floats = array.array('f', (v / 32768.0 for v in ints))
    return floats


@dataclass(frozen=True, slots=True)
class SttEvent:
    status: str  # 'partial' | 'final'
    text: str
    start_offset: int
    end_offset: int


class SessionConfigError(RuntimeError):
    """Model missing or dependencies not installed."""


class ServerSession:
    """Stateful streaming recognizer for exactly one connection."""

    def __init__(self, *, model_dir: Path, num_threads: int = 2,
                 recognizer_factory=None, clock=time.monotonic):
        self.model_dir = Path(model_dir)
        self.num_threads = num_threads
        self._recognizer_factory = recognizer_factory
        self._clock = clock
        self._recognizer = None
        self._stream = None
        self._np = None
        self._fed = 0  # samples received since connection origin
        self._last_final_end: int | None = None
        self._last_partial = ''
        self._decode_ms = 0.0

    @property
    def fed(self) -> int:
        return self._fed

    @property
    def decode_ms(self) -> float:
        return self._decode_ms

    def start(self) -> None:
        if self._recognizer_factory is not None:
            self._recognizer = self._recognizer_factory()
        else:
            missing = [name for name in _MODEL_FILES
                       if not (self.model_dir / name).is_file()]
            if missing:
                raise SessionConfigError(
                    f'missing STT model files in {self.model_dir}: {missing}')
            try:
                import sherpa_onnx
            except ImportError as exc:
                raise SessionConfigError(
                    'sherpa-onnx is required: pip install -e .') from exc
            self._np = __import__('numpy')
            self._recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
                tokens=str(self.model_dir / 'tokens.txt'),
                encoder=str(self.model_dir / 'encoder.int8.onnx'),
                decoder=str(self.model_dir / 'decoder.onnx'),
                joiner=str(self.model_dir / 'joiner.int8.onnx'),
                num_threads=self.num_threads, provider='cpu',
                sample_rate=RATE, feature_dim=80,
                enable_endpoint_detection=True,
                rule1_min_trailing_silence=2.4,
                rule2_min_trailing_silence=1.2,
                rule3_min_utterance_length=300)
        self._stream = self._recognizer.create_stream()

    def _samples(self, pcm: bytes):
        return _pcm_to_floats(pcm, self._np)

    def _decode(self) -> None:
        recognizer = self._recognizer
        assert recognizer is not None
        started = self._clock()
        while recognizer.is_ready(self._stream):
            recognizer.decode_stream(self._stream)
        self._decode_ms += (self._clock() - started) * 1000

    def _utterance_start(self) -> int:
        return self._last_final_end if self._last_final_end is not None else 0

    def _flush(self) -> list[SttEvent]:
        recognizer = self._recognizer
        assert recognizer is not None
        events: list[SttEvent] = []
        text = recognizer.get_result(self._stream).strip()
        if text:
            events.append(SttEvent(FINAL, text, self._utterance_start(),
                                   self._fed))
            self._last_final_end = self._fed
        recognizer.reset(self._stream)
        self._last_partial = ''
        return events

    def feed_samples(self, pcm: bytes) -> list[SttEvent]:
        if self._stream is None:
            raise RuntimeError('session not started')
        if not pcm or len(pcm) % 2:
            raise ValueError('pcm must be non-empty int16 frames')
        self._stream.accept_waveform(RATE, self._samples(pcm))
        self._fed += len(pcm) // 2
        self._decode()
        recognizer = self._recognizer
        assert recognizer is not None
        text = recognizer.get_result(self._stream).strip()
        events: list[SttEvent] = []
        if recognizer.is_endpoint(self._stream):
            if text:
                events.append(SttEvent(FINAL, text, self._utterance_start(),
                                       self._fed))
                self._last_final_end = self._fed
            recognizer.reset(self._stream)
            self._last_partial = ''
        elif text != self._last_partial:
            events.append(SttEvent(PARTIAL, text, self._utterance_start(),
                                   self._fed))
            self._last_partial = text
        return events

    def feed_gap(self) -> list[SttEvent]:
        """Explicit pause boundary: close the open utterance."""
        if self._stream is None:
            return []
        return self._flush()

    def close(self) -> list[SttEvent]:
        """End of stream: pad and flush the trailing utterance."""
        if self._stream is None:
            return []
        pad_n = int(0.7 * RATE)
        if self._np is not None:
            pad = self._np.zeros(pad_n, dtype=self._np.float32)
        else:
            import array

            pad = array.array('f', [0.0]) * pad_n
        self._stream.accept_waveform(RATE, pad)
        self._stream.input_finished()
        self._decode()
        events: list[SttEvent] = []
        recognizer = self._recognizer
        assert recognizer is not None
        text = recognizer.get_result(self._stream).strip()
        if text:
            events.append(SttEvent(FINAL, text, self._utterance_start(),
                                   self._fed))
        self._stream = None
        return events

    def cancel(self) -> None:
        self._stream = None
        self._recognizer = None


class FireRedSession:
    """VAD-segmented offline session (fire-red-asr2 AED): finals only.

    silero VAD closes a segment on trailing silence or the max-speech
    cap; the closed segment is decoded as a whole by the offline
    recognizer and emitted as one final. ``gap``/``stop`` flush the open
    segment. Utterances are VAD pause boundaries, not sentences, and the
    model outputs no punctuation (TODO T13).
    """

    def __init__(self, *, model_dir: Path, num_threads: int = 2,
                 recognizer_factory=None, vad_factory=None,
                 clock=time.monotonic):
        self.model_dir = Path(model_dir)
        self.num_threads = num_threads
        self._recognizer_factory = recognizer_factory
        self._vad_factory = vad_factory
        self._clock = clock
        self._recognizer = None
        self._vad = None
        self._np = None
        self._fed = 0  # samples received since connection origin
        self._decode_ms = 0.0

    @property
    def fed(self) -> int:
        return self._fed

    @property
    def decode_ms(self) -> float:
        return self._decode_ms

    def start(self) -> None:
        if self._recognizer_factory is not None:
            self._recognizer = self._recognizer_factory()
        else:
            missing = [name for name in FIRE_RED_FILES
                       if not (self.model_dir / name).is_file()]
            if missing:
                raise SessionConfigError(
                    f'missing STT model files in {self.model_dir}: {missing}')
            try:
                import sherpa_onnx
            except ImportError as exc:
                raise SessionConfigError(
                    'sherpa-onnx is required: pip install -e .') from exc
            self._np = __import__('numpy')
            self._recognizer = sherpa_onnx.OfflineRecognizer.from_fire_red_asr(
                encoder=str(self.model_dir / 'encoder.int8.onnx'),
                decoder=str(self.model_dir / 'decoder.int8.onnx'),
                tokens=str(self.model_dir / 'tokens.txt'),
                num_threads=self.num_threads, provider='cpu')
        if self._vad_factory is not None:
            self._vad = self._vad_factory()
        else:
            import sherpa_onnx

            silero = sherpa_onnx.SileroVadModelConfig(
                model=str(self.model_dir / 'silero_vad.onnx'),
                threshold=VAD_THRESHOLD,
                min_silence_duration=VAD_MIN_SILENCE_S,
                min_speech_duration=VAD_MIN_SPEECH_S,
                window_size=512,
                max_speech_duration=VAD_MAX_SPEECH_S)
            self._vad = sherpa_onnx.VoiceActivityDetector(
                sherpa_onnx.VadModelConfig(silero_vad=silero,
                                           sample_rate=RATE, num_threads=1,
                                           provider='cpu'),
                buffer_size_in_seconds=VAD_BUFFER_S)

    def _samples(self, pcm: bytes):
        return _pcm_to_floats(pcm, self._np)

    def _decode(self, samples) -> str:
        recognizer = self._recognizer
        assert recognizer is not None
        stream = recognizer.create_stream()
        stream.accept_waveform(RATE, samples)
        started = self._clock()
        recognizer.decode_stream(stream)
        self._decode_ms += (self._clock() - started) * 1000
        return getattr(stream.result, 'text', '').strip()

    def _drain(self) -> list[SttEvent]:
        events: list[SttEvent] = []
        vad = self._vad
        assert vad is not None
        while not vad.empty():
            segment = vad.front
            text = self._decode(segment.samples)
            if text:
                events.append(SttEvent(FINAL, text, segment.start,
                                       segment.start + len(segment.samples)))
            vad.pop()
        return events

    def feed_samples(self, pcm: bytes) -> list[SttEvent]:
        if self._vad is None:
            raise RuntimeError('session not started')
        if not pcm or len(pcm) % 2:
            raise ValueError('pcm must be non-empty int16 frames')
        self._vad.accept_waveform(self._samples(pcm))
        self._fed += len(pcm) // 2
        return self._drain()

    def feed_gap(self) -> list[SttEvent]:
        """Explicit pause boundary: close the open VAD segment."""
        if self._vad is None:
            return []
        self._vad.flush()
        return self._drain()

    def close(self) -> list[SttEvent]:
        """End of stream: flush the open VAD segment, if any."""
        if self._vad is None:
            return []
        self._vad.flush()
        return self._drain()

    def cancel(self) -> None:
        self._vad = None
        self._recognizer = None
