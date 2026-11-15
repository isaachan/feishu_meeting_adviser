"""Capture only Feishu process output using Windows process loopback."""
import struct
import tempfile
import threading
import time
from pathlib import Path

from .frames import AudioFrame, SourceEvent
from .sources import AudioSource


def read_capture(path):
    """Read the process-audio-capture WAV and convert it to 16 kHz mono."""
    import numpy as np

    data = Path(path).read_bytes()
    if len(data) < 12 or data[:4] != b'RIFF' or data[8:12] != b'WAVE':
        raise ValueError('invalid capture WAV')
    pos, fmt, pcm = 12, None, None
    while pos + 8 <= len(data):
        chunk, size = struct.unpack_from('<4sI', data, pos)
        body = data[pos + 8:pos + 8 + size]
        if chunk == b'fmt ' and len(body) >= 16:
            tag, channels, rate, _, _, bits = struct.unpack_from('<HHIIHH', body)
            floating = tag == 3 or (tag == 0xfffe and len(body) >= 40 and body[24:26] == b'\x03\x00')
            fmt = (channels, rate, bits, floating)
        elif chunk == b'data':
            pcm = body
            break
        pos += 8 + size + (size & 1)
    if fmt is None or pcm is None:
        raise ValueError('capture WAV missing fmt or data')
    channels, rate, bits, floating = fmt
    if not channels or not rate:
        raise ValueError('invalid capture WAV format')
    if floating and bits == 32:
        samples = np.frombuffer(pcm[:len(pcm) // 4 * 4], dtype='<f4')
    elif not floating and bits == 16:
        samples = np.frombuffer(pcm[:len(pcm) // 2 * 2], dtype='<i2').astype(np.float32) / 32768
    else:
        raise ValueError(f'unsupported capture WAV format: {bits} bits')
    samples = samples[:len(samples) // channels * channels].reshape(-1, channels).mean(axis=1)
    if rate != 16000 and len(samples) > 1:
        positions = np.arange(0, len(samples) - 1, rate / 16000)
        samples = np.interp(positions, np.arange(len(samples)), samples)
    return np.asarray(samples, dtype=np.float32)


def find_candidates(capture_class):
    processes = [p for p in capture_class.enumerate_audio_processes()
                 if any(hint in (p.name or '').lower() for hint in ('feishu', 'lark'))]
    processes.sort(key=lambda p: 0 if any(hint in (getattr(p, 'window_title', '') or '').lower()
                                           for hint in ('会议', 'meeting', '视频', 'video')) else 1)
    return [p.pid for p in processes]


class WindowsLoopbackSource(AudioSource):
    """Per-process capture; the upstream DLL writes short WAV segments."""

    rate = 16000
    channels = 1

    def __init__(self, source_id='loopback', *, chunk_seconds=2.5, clock=time.monotonic):
        self.source_id = source_id
        self.chunk_seconds = chunk_seconds
        self.clock = clock
        self._stop = threading.Event()
        self._thread = None
        self._sink = None
        self._seq = 0
        self._offset = 0
        self._paused = False
        self._status = '等待飞书声音'

    def start(self, sink):
        from process_audio_capture import ProcessAudioCapture, PacCaptureMode

        if not ProcessAudioCapture.is_supported():
            raise RuntimeError('当前 Windows 不支持进程环回（需要 Windows 10 2004 或更新版本）')
        self._sink = sink
        self._capture_class = ProcessAudioCapture
        self._mode = PacCaptureMode.INCLUDE
        self._sink(SourceEvent(self.source_id, 'source_started', self.clock(),
                               {'rate': self.rate, 'channels': self.channels}))
        self._thread = threading.Thread(target=self._run, daemon=True, name='windows-loopback')
        self._thread.start()

    def _push(self, samples):
        for i in range(0, len(samples), 1600):
            if self._stop.is_set() or self._paused:
                break
            block = samples[i:i + 1600]
            self._seq += 1
            self._sink(AudioFrame(self.source_id, self._seq, self._offset,
                                  self.rate, 1, block.tobytes(), self.clock()))
            self._offset += len(block)

    def _run(self):
        import numpy as np

        candidates, pid, silent, cycles = [], None, 0, 0
        with tempfile.TemporaryDirectory(prefix='feishu-loopback-') as tmp:
            path = Path(tmp) / 'capture.wav'
            while not self._stop.is_set():
                try:
                    if self._paused:
                        self._stop.wait(0.1)
                        continue
                    if cycles % 8 == 0 or not candidates or pid not in candidates:
                        candidates = find_candidates(self._capture_class)
                        if pid not in candidates:
                            pid = candidates[0] if candidates else None
                    if pid is None:
                        self._status = '等待飞书声音'
                        self._push(np.zeros(1600, dtype=np.float32))
                        self._stop.wait(0.1)
                        continue
                    path.unlink(missing_ok=True)
                    capture = self._capture_class(pid=pid, output_path=str(path), mode=self._mode)
                    capture.start()
                    try:
                        self._status = f'采集飞书进程 {pid}'
                        self._stop.wait(self.chunk_seconds)
                    finally:
                        capture.stop()
                    if path.exists():
                        pcm = read_capture(path)
                        path.unlink(missing_ok=True)
                        silent = silent + 1 if len(pcm) and float(np.max(np.abs(pcm))) < 1e-4 else 0
                        self._push(pcm)
                    cycles += 1
                    if silent >= 4 and len(candidates) > 1:
                        pid = candidates[(candidates.index(pid) + 1) % len(candidates)]
                        silent = 0
                except Exception as exc:
                    self._status = f'飞书进程采集失败：{exc}'
                    self._sink(SourceEvent(self.source_id, 'device_warning', self.clock(),
                                           {'message': self._status}))
                    candidates, pid = [], None
                    self._stop.wait(1)
        self._sink(SourceEvent(self.source_id, 'source_stopped', self.clock()))

    def stats(self):
        return {'state': self._status}

    def pause(self):
        if not self._paused:
            self._paused = True
            self._sink(SourceEvent(self.source_id, 'gap_begin', self.clock()))

    def resume(self):
        if self._paused:
            self._paused = False
            self._sink(SourceEvent(self.source_id, 'gap_end', self.clock()))

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
