"""AudioSource implementations: manual-switch microphone and test fake.

The ScreenCaptureKit loopback source lives in sck_capture.py; all sources
share this minimal protocol so platform details stay behind one seam.
"""
from abc import ABC, abstractmethod
import struct
import threading
import time
from typing import Callable

from .frames import AudioFrame, SourceEvent

Sink = Callable[[object], None]


class AudioSource(ABC):
    """A stoppable, pausable producer of AudioFrame/SourceEvent items.

    Producers must never block on consumer speed: they hand each item to
    the sink and move on. Pause/resume must emit gap events instead of
    silently stretching the sample timeline.
    """

    source_id: str
    rate: int
    channels: int

    @abstractmethod
    def start(self, sink: Sink) -> None:
        """Begin producing; must return once the source is live (or raise)."""

    @abstractmethod
    def pause(self) -> None:
        """Stop producing frames and emit a gap event."""

    @abstractmethod
    def resume(self) -> None:
        """Resume producing frames on the same cumulative sample timeline."""

    @abstractmethod
    def stop(self) -> None:
        """Idempotently stop; the producer emits a final stopped event."""

    def join(self, timeout: float | None = None) -> None:  # noqa: ARG002
        """Wait for producer threads; default sources have none."""


class FakeSource(AudioSource):
    """Deterministic source for tests: frames are pushed by the test itself."""

    def __init__(self, source_id: str = 'fake', *, rate: int = 16000,
                 channels: int = 1, clock=time.monotonic):
        self.source_id = source_id
        self.rate = rate
        self.channels = channels
        self._clock = clock
        self._sink: Sink | None = None
        self._seq = 0
        self._offset = 0
        self._paused = False
        self._stopped = False
        self._lock = threading.Lock()

    def start(self, sink: Sink) -> None:
        with self._lock:
            if self._stopped:
                raise RuntimeError('source already stopped')
            self._sink = sink
        sink(SourceEvent(self.source_id, 'source_started', self._clock(),
                         {'rate': self.rate, 'channels': self.channels}))

    def push_frames(self, count: int, value: float = 0.25) -> int:
        """Push ``count`` audio frames as one chunk; returns the new offset."""
        with self._lock:
            if self._stopped or self._sink is None:
                raise RuntimeError('source not running')
            self._seq += 1
            data = struct.pack('<f', max(-1.0, min(1.0, value))) * count
            self._sink(AudioFrame(self.source_id, self._seq, self._offset,
                                  self.rate, self.channels, data, self._clock()))
            self._offset += count
            return self._offset

    def pause(self) -> None:
        with self._lock:
            if self._stopped or self._paused or self._sink is None:
                return
            self._paused = True
            sink = self._sink
        sink(SourceEvent(self.source_id, 'gap_begin', self._clock()))

    def resume(self) -> None:
        with self._lock:
            if self._stopped or not self._paused or self._sink is None:
                return
            self._paused = False
            sink = self._sink
        sink(SourceEvent(self.source_id, 'gap_end', self._clock()))

    def stop(self) -> None:
        with self._lock:
            if self._stopped or self._sink is None:
                self._stopped = True
                return
            self._stopped = True
            sink = self._sink
        sink(SourceEvent(self.source_id, 'source_stopped', self._clock()))

    @property
    def paused(self) -> bool:
        return self._paused

    @property
    def stopped(self) -> bool:
        return self._stopped

    @property
    def offset(self) -> int:
        return self._offset


class MicSource(AudioSource):
    """Default-input microphone behind a manual switch (privacy: off by user).

    pause/resume map to stopping/starting the PortAudio stream; the sample
    timeline keeps accumulating across pauses, and gap events mark the
    intervals explicitly so no timestamps are fabricated.
    """

    def __init__(self, source_id: str = 'mic', *, rate: int = 16000,
                 channels: int = 1, blocksize: int = 1024, device: int | None = None,
                 clock=time.monotonic):
        self.source_id = source_id
        self.rate = rate
        self.channels = channels
        self.blocksize = blocksize
        self.device = device
        self._clock = clock
        self._sink: Sink | None = None
        self._seq = 0
        self._offset = 0
        self._stream = None
        self._running = False
        self._lock = threading.Lock()

    def start(self, sink: Sink) -> None:
        import sounddevice  # imported here: opening hardware needs the lib, importing the module does not

        with self._lock:
            if self._running:
                return
            self._sink = sink
        stream = sounddevice.RawInputStream(
            samplerate=self.rate, channels=self.channels, dtype='float32',
            blocksize=self.blocksize, device=self.device, callback=self._on_audio)
        try:
            stream.start()
        except Exception:
            sink(SourceEvent(self.source_id, 'device_error', self._clock(),
                             {'message': 'failed to open microphone input'}))
            raise
        self._stream = stream
        self._running = True
        sink(SourceEvent(self.source_id, 'source_started', self._clock(),
                         {'rate': self.rate, 'channels': self.channels,
                          'blocksize': self.blocksize}))

    def _on_audio(self, indata, frame_count, _time_info, status) -> None:  # noqa: ARG002
        with self._lock:
            sink = self._sink
            if not self._running or sink is None:
                return
            self._seq += 1
            seq, offset = self._seq, self._offset
            self._offset += frame_count
        if status:
            sink(SourceEvent(self.source_id, 'device_warning', self._clock(),
                             {'message': str(status)}))
        sink(AudioFrame(self.source_id, seq, offset, self.rate, self.channels,
                        bytes(indata), self._clock()))

    def pause(self) -> None:
        with self._lock:
            sink = self._sink
            stream = self._stream
            if not self._running or sink is None or stream is None:
                return
            self._running = False
        stream.stop()
        sink(SourceEvent(self.source_id, 'gap_begin', self._clock()))

    def resume(self) -> None:
        with self._lock:
            sink = self._sink
            stream = self._stream
            if self._running or sink is None or stream is None:
                return
            self._running = True
        stream.start()
        sink(SourceEvent(self.source_id, 'gap_end', self._clock()))

    def stop(self) -> None:
        with self._lock:
            sink = self._sink
            stream = self._stream
            was_running = self._running
            self._running = False
        if stream is not None:
            if was_running:
                stream.stop()
            stream.close()
        self._stream = None
        if sink is not None:
            sink(SourceEvent(self.source_id, 'source_stopped', self._clock()))

    def join(self, timeout: float | None = None) -> None:
        stream = self._stream
        if stream is not None:
            stream.close()
