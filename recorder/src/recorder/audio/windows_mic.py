"""Windows microphone source: UIA mute gate and default-device monitoring."""
import threading
import time

from .frames import AudioFrame, SourceEvent
from .sources import AudioSource
from .windows_mute import FeishuMuteDetector


class WindowsMicBackend:
    """Discover the Windows default endpoint separately from PortAudio's cache."""

    def default_device(self):
        import soundcard

        mic = soundcard.default_microphone()
        if mic is None:
            return None
        return {'id': str(mic.id), 'name': mic.name}

    def open(self, device, callback):
        import sounddevice as sd

        def resolve():
            target = device['name'].casefold()
            matches = []
            apis = sd.query_hostapis()
            for index, item in enumerate(sd.query_devices()):
                if item['max_input_channels'] < 1:
                    continue
                name = item['name'].casefold()
                if target not in name and name not in target:
                    continue
                api = apis[item['hostapi']]['name']
                score = 2 if 'WASAPI' in api else 1
                matches.append((score, index, item))
            return max(matches, default=None, key=lambda row: row[0])

        match = resolve()
        if match is None:
            # Refresh only while this source has no active stream.
            sd._terminate()
            sd._initialize()
            match = resolve()
        if match is None:
            raise RuntimeError(f"找不到默认麦克风：{device['name']}")
        _, index, info = match
        candidates = (48000, 44100, 32000, 16000, int(info['default_samplerate']))
        for rate in dict.fromkeys(candidates):
            try:
                sd.check_input_settings(device=index, samplerate=rate, channels=1, dtype='float32')
                stream = sd.InputStream(device=index, samplerate=rate, channels=1,
                                        dtype='float32', callback=callback,
                                        blocksize=max(1, int(rate * 0.03)))
                return stream, rate
            except Exception:
                continue
        raise RuntimeError(f"无法打开麦克风：{device['name']}")


class WindowsMicSource(AudioSource):
    """Only open the microphone while a fresh detector reading says unmuted."""

    rate = 16000
    channels = 1

    def __init__(self, source_id='mic', *, detector=None, backend=None,
                 interval=0.2, clock=time.monotonic):
        self.source_id = source_id
        self.detector = detector or FeishuMuteDetector()
        self.backend = backend or WindowsMicBackend()
        self.interval = interval
        self.clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._stream = None
        self._device = None
        self._sink = None
        self._active = False
        self._manual_pause = False
        self._seq = 0
        self._offset = 0
        self._input_rate = 16000
        self._buffer = None
        self._position = 0.0
        self._candidate = None
        self._candidate_since = 0.0
        self._status = {'state': 'unknown', 'detail': '等待飞书麦克风状态', 'device': None}

    def start(self, sink):
        self._sink = sink
        sink(SourceEvent(self.source_id, 'source_started', self.clock(),
                         {'rate': self.rate, 'channels': self.channels}))
        self.detector.start()
        self._thread = threading.Thread(target=self._run, daemon=True, name='windows-mic')
        self._thread.start()

    def _emit(self, kind, **detail):
        self._sink(SourceEvent(self.source_id, kind, self.clock(), detail))

    def _callback(self, indata, frame_count, time_info, status):
        import numpy as np

        state = self.detector.get_state()
        with self._lock:
            if (not self._active or self._manual_pause or not state.get('known')
                    or state.get('muted') is not False):
                return
            values = np.asarray(indata[:, 0], dtype=np.float32)
            if self._buffer is not None:
                values = np.concatenate((self._buffer, values))
            if len(values) < 2:
                self._buffer = values.copy()
                return
            ratio = self._input_rate / self.rate
            count = max(0, int((len(values) - 1 - self._position) / ratio) + 1)
            if not count:
                self._buffer = values.copy()
                return
            positions = self._position + np.arange(count) * ratio
            out = np.interp(positions, np.arange(len(values)), values).astype(np.float32)
            position = float(positions[-1]) + ratio
            cut = min(max(0, int(position) - 1), len(values) - 1)
            self._buffer = values[cut:].copy()
            self._position = position - cut
            self._seq += 1
            frame = AudioFrame(self.source_id, self._seq, self._offset,
                               self.rate, 1, out.tobytes(), self.clock())
            self._offset += len(out)
            self._sink(frame)

    def _close_stream(self):
        with self._lock:
            stream, self._stream = self._stream, None
            active, self._active = self._active, False
            self._device = None
            self._buffer = None
            self._position = 0.0
        if stream is not None:
            try:
                stream.stop()
            finally:
                stream.close()
        if active:
            self._emit('gap_begin')

    def _open_stream(self, device):
        stream, rate = self.backend.open(device, self._callback)
        try:
            with self._lock:
                self._input_rate = rate
                self._stream = stream
                self._device = device
                self._active = True
                self._buffer = None
                self._position = 0.0
                self._emit('gap_end')
            stream.start()
        except Exception:
            self._close_stream()
            raise

    def _tick(self):
        state = self.detector.get_state()
        allowed = state.get('known') and state.get('muted') is False and not self._manual_pause
        if not allowed:
            self._close_stream()
            mode = 'muted' if state.get('known') and state.get('muted') else 'unknown'
            self._status = {'state': mode, 'detail': state.get('detail', ''), 'device': None}
            return
        try:
            device = self.backend.default_device()
            if not device:
                self._close_stream()
                self._status = {'state': 'no_device', 'detail': '没有默认麦克风', 'device': None}
                return
            if self._device and self._device['id'] != device['id']:
                if self._candidate != device['id']:
                    self._candidate = device['id']
                    self._candidate_since = self.clock()
                    return
                if self.clock() - self._candidate_since < 2.0:
                    return
                self._close_stream()
            else:
                self._candidate = None
            if self._stream is None:
                self._open_stream(device)
            self._status = {'state': 'open', 'detail': state.get('detail', ''),
                            'device': device['name']}
        except Exception as exc:
            self._close_stream()
            self._status = {'state': 'error', 'detail': str(exc), 'device': None}

    def _run(self):
        # soundcard uses COM on this thread.
        initialized = False
        try:
            import ctypes
            initialized = ctypes.windll.ole32.CoInitializeEx(None, 0) in (0, 1)
        except Exception:
            pass
        try:
            while not self._stop.is_set():
                self._tick()
                self._stop.wait(self.interval)
        finally:
            self._close_stream()
            if initialized:
                import ctypes
                ctypes.windll.ole32.CoUninitialize()

    def stats(self):
        return dict(self._status)

    def pause(self):
        self._manual_pause = True
        self._close_stream()

    def resume(self):
        self._manual_pause = False

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.detector.stop()
        self._emit('source_stopped')
