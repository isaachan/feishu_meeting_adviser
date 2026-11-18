"""macOS microphone capture gated by the local Feishu meeting mute button."""
import threading
import time

from .frames import AudioFrame, SourceEvent
from .macos_mute import MacFeishuMuteDetector
from .sources import AudioSource


class MacMicBackend:
    def open(self, callback):
        import sounddevice as sd

        return sd.RawInputStream(samplerate=16000, channels=1, dtype='float32',
                                 blocksize=480, callback=callback)


class MacFeishuMicSource(AudioSource):
    """Open the input device only after Feishu is confirmed unmuted."""

    rate = 16000
    channels = 1

    def __init__(self, source_id='mic', *, detector=None, backend=None,
                 interval=0.2, clock=time.monotonic):
        self.source_id = source_id
        self.detector = detector or MacFeishuMuteDetector()
        self.backend = backend or MacMicBackend()
        self.interval = interval
        self.clock = clock
        self._sink = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._stream = None
        self._active = False
        self._manual_pause = False
        self._finished = False
        self._seq = 0
        self._offset = 0
        self._silence_at = None
        self._status = {'state': 'unknown', 'detail': '等待飞书麦克风状态'}

    def start(self, sink):
        self._sink = sink
        sink(SourceEvent(self.source_id, 'source_started', self.clock(),
                         {'rate': self.rate, 'channels': self.channels}))
        self.detector.start()
        self._thread = threading.Thread(target=self._run, name='macos-mic', daemon=True)
        self._thread.start()

    def _emit(self, kind, **detail):
        sink = self._sink
        if sink is not None:
            sink(SourceEvent(self.source_id, kind, self.clock(), detail))

    def _callback(self, indata, frame_count, _time_info, status):
        state = self.detector.get_state()
        with self._lock:
            sink = self._sink
            if (sink is None or not self._active or self._manual_pause or not state.get('known')
                    or state.get('muted') is not False):
                return
            self._seq += 1
            frame = AudioFrame(self.source_id, self._seq, self._offset,
                               self.rate, 1, bytes(indata), self.clock())
            self._offset += frame_count
            sink(frame)
            if status:
                self._emit('device_warning', message=str(status))

    def _close_stream(self):
        with self._lock:
            stream, self._stream = self._stream, None
            active, self._active = self._active, False
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        if active:
            self._emit('gap_begin')

    def _open_stream(self):
        stream = self.backend.open(self._callback)
        with self._lock:
            self._stream = stream
            self._active = True
            self._silence_at = None
        try:
            stream.start()
        except Exception:
            self._close_stream()
            raise
        self._emit('gap_end')

    def _silence(self):
        now = self.clock()
        with self._lock:
            sink = self._sink
            if sink is None:
                return
            if self._active:
                self._silence_at = None
                return
            if self._silence_at is None:
                self._silence_at = now
                return
            count = min(int((now - self._silence_at) * self.rate), self.rate)
            if count < 1600:
                return
            self._silence_at += count / self.rate
            self._seq += 1
            sink(AudioFrame(self.source_id, self._seq, self._offset,
                            self.rate, 1, b'\0' * count * 4, now))
            self._offset += count

    def _tick(self):
        state = self.detector.get_state()
        allowed = state.get('known') and state.get('muted') is False and not self._manual_pause
        if not allowed:
            self._close_stream()
            mode = 'muted' if state.get('known') and state.get('muted') else 'unknown'
            self._status = {'state': mode, 'detail': state.get('detail', '')}
        elif self._stream is None:
            try:
                self._open_stream()
                self._status = {'state': 'open', 'detail': state.get('detail', '')}
            except Exception as exc:
                self._status = {'state': 'error', 'detail': str(exc)}
                self._emit('device_error', message=str(exc))
        self._silence()

    def _run(self):
        try:
            while not self._stop.is_set():
                self._tick()
                self._stop.wait(self.interval)
        finally:
            self._close_stream()

    def stats(self):
        return dict(self._status)

    def pause(self):
        self._manual_pause = True
        self._close_stream()

    def resume(self):
        self._manual_pause = False

    def stop(self):
        if self._finished:
            return
        self._finished = True
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.detector.stop()
        if self._sink is not None:
            self._emit('source_stopped')
