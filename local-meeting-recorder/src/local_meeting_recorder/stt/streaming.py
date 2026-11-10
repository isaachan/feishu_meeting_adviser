"""Streaming STT sessions (stage 5.10): feed frames, get partial/final events.

Interface extension frozen 2026-11-10:
- one StreamingSession per source, fed AudioFrames in capture order;
- events carry source-timeline sample offsets (start/end of the utterance);
- partial events never reach the caption contract (advisor must not act on
  them); final events become caption events;
- gaps/pauses are explicit: feed_gap() closes the open utterance at the
  boundary, never fabricating a continuous timeline.

The first implementation wraps the sherpa-onnx streaming Zipformer
transducer (zh-en-punct int8, 2026-06-05) already validated in
studies/local-meeting; the implementation is copied/adapted here on purpose
(module independence), not imported.
"""
from dataclasses import dataclass
from pathlib import Path
import time

from ..audio.frames import AudioFrame

RATE = 16000
PARTIAL = 'partial'
FINAL = 'final'


@dataclass(frozen=True, slots=True)
class SttEvent:
    status: str  # 'partial' | 'final'
    text: str
    start_offset: int
    end_offset: int


class StreamingSession:
    """One source's stateful streaming recognizer session."""

    def start(self) -> None: ...
    def feed(self, frame: AudioFrame) -> list[SttEvent]: ...
    def feed_gap(self) -> list[SttEvent]: ...
    def close(self) -> list[SttEvent]: ...


_MODEL_FILES = ('tokens.txt', 'encoder.int8.onnx', 'decoder.onnx',
                'joiner.int8.onnx')


class LocalSherpaSession(StreamingSession):
    """sherpa-onnx online transducer with endpoint detection and offsets."""

    def __init__(self, source_id: str, *, model_dir: Path, num_threads: int = 2,
                 recognizer=None, clock=time.monotonic):
        self.source_id = source_id
        self.model_dir = Path(model_dir)
        self.num_threads = num_threads
        self._recognizer_factory = recognizer
        self._clock = clock
        self._recognizer = None
        self._stream = None
        self._np = None
        self._base = None  # source sample offset where the session began
        self._fed = 0      # samples fed since _base
        self._last_final_end = None
        self._last_partial = ''
        self._finals = 0
        self._decode_ms = 0.0

    @property
    def decode_ms(self) -> float:
        return self._decode_ms

    def start(self) -> None:
        if self._recognizer_factory is None:
            missing = [name for name in _MODEL_FILES
                       if not (self.model_dir / name).is_file()]
            if missing:
                raise FileNotFoundError(
                    f'missing STT model files in {self.model_dir}: {missing}')
            try:
                import sherpa_onnx
            except ImportError as exc:
                raise RuntimeError(
                    'local STT needs sherpa-onnx: pip install -e ".[local]"'
                ) from exc
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
        else:
            self._recognizer = self._recognizer_factory()
        self._stream = self._recognizer.create_stream()

    # -- internal helpers ---------------------------------------------------

    def _samples(self, pcm: bytes):
        # frames carry interleaved float32 PCM (capture contract); sherpa
        # consumes float32 samples directly
        if self._np is not None:
            return self._np.frombuffer(pcm, dtype='<f4')
        import array

        floats = array.array('f')
        floats.frombytes(pcm)
        return floats

    def _decode(self) -> None:
        recognizer = self._recognizer
        assert recognizer is not None
        started = self._clock()
        while recognizer.is_ready(self._stream):
            recognizer.decode_stream(self._stream)
        self._decode_ms += (self._clock() - started) * 1000

    def _current(self) -> int:
        return (self._base or 0) + self._fed

    def _events(self, *, endpoint: bool) -> list[SttEvent]:
        recognizer = self._recognizer
        assert recognizer is not None
        text = recognizer.get_result(self._stream).strip()
        events: list[SttEvent] = []
        if endpoint:
            if text:
                start = self._last_final_end if self._last_final_end is not None \
                    else (self._base or 0)
                self._finals += 1
                events.append(SttEvent(FINAL, text, start, self._current()))
                self._last_final_end = self._current()
            recognizer.reset(self._stream)
            self._last_partial = ''
        elif text != self._last_partial:
            start = self._last_final_end if self._last_final_end is not None \
                else (self._base or 0)
            events.append(SttEvent(PARTIAL, text, start, self._current()))
            self._last_partial = text
        return events

    # -- StreamingSession API ----------------------------------------------

    def feed(self, frame: AudioFrame) -> list[SttEvent]:
        if self._stream is None:
            raise RuntimeError('session not started')
        if frame.channels != 1 or frame.rate != RATE:
            raise ValueError('streaming STT expects 16 kHz mono frames')
        events: list[SttEvent] = []
        if self._base is None:
            self._base = frame.sample_offset
        elif frame.sample_offset != self._base + self._fed:
            # unreported discontinuity: close the open utterance, re-anchor
            events += self.feed_gap()
            self._base = frame.sample_offset
            self._fed = 0
        samples = self._samples(frame.data)
        self._stream.accept_waveform(RATE, samples)
        self._fed += len(samples)
        self._decode()
        endpoint = self._recognizer is not None and \
            self._recognizer.is_endpoint(self._stream)
        return events + self._events(endpoint=endpoint)

    def feed_gap(self) -> list[SttEvent]:
        """Explicit pause: flush the open utterance at the boundary."""
        if self._stream is None:
            return []
        recognizer = self._recognizer
        assert recognizer is not None
        events: list[SttEvent] = []
        text = recognizer.get_result(self._stream).strip()
        if text:
            start = self._last_final_end if self._last_final_end is not None \
                else (self._base or 0)
            self._finals += 1
            events.append(SttEvent(FINAL, text, start, self._current()))
            self._last_final_end = self._current()
        recognizer.reset(self._stream)
        self._last_partial = ''
        # the next utterance starts at the (possibly re-anchored) base
        self._last_final_end = None
        return events

    def close(self) -> list[SttEvent]:
        """End of stream: pad, flush the trailing utterance."""
        if self._stream is None:
            return []
        import array as _array

        pad_n = int(0.7 * RATE)
        if self._np is not None:
            pad = self._np.zeros(pad_n, dtype=self._np.float32)
        else:
            pad = _array.array('f', [0.0]) * pad_n
        self._stream.accept_waveform(RATE, pad)
        self._stream.input_finished()
        self._decode()
        recognizer = self._recognizer
        assert recognizer is not None
        events: list[SttEvent] = []
        text = recognizer.get_result(self._stream).strip()
        if text:
            start = self._last_final_end if self._last_final_end is not None \
                else (self._base or 0)
            self._finals += 1
            events.append(SttEvent(FINAL, text, start, self._current()))
        self._stream = None
        return events
