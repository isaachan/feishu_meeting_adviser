"""Windows 进程级音频环回（AUDIOCLIENT_PROCESS_LOOPBACK）采集。

只抓指定进程（默认飞书）输出的声音，不混入微信、通知等其它软件的声音。
底层用 process-audio-capture 的 DLL：它只支持「录到 WAV 文件 + 电平回调」，
所以这里按固定时长分段录、分段读，再把音频喂给现有 VAD/ASR 管线。

分段之间约有 50ms 的启动间隙（实测约 2% 音频损失），对字幕影响很小。
"""

from __future__ import annotations

import os
import struct
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable

import numpy as np

from audio_capture import TARGET_SR, Resampler

try:
    from process_audio_capture import (
        ProcessAudioCapture,
        ProcessAudioCaptureError,
        PacCaptureMode,
    )
except Exception:  # 未安装时给出明确提示，而不是导入失败
    ProcessAudioCapture = None
    ProcessAudioCaptureError = Exception
    PacCaptureMode = None

FEISHU_HINTS = ("feishu", "lark")


def is_supported() -> bool:
    if ProcessAudioCapture is None:
        return False
    try:
        return bool(ProcessAudioCapture.is_supported())
    except Exception:
        return False


def enumerate_audio_processes():
    if ProcessAudioCapture is None:
        return []
    try:
        return ProcessAudioCapture.enumerate_audio_processes()
    except Exception:
        return []


def find_feishu_pids() -> list[int]:
    return [
        p.pid
        for p in enumerate_audio_processes()
        if any(h in (p.name or "").lower() for h in FEISHU_HINTS)
    ]


def resolve_feishu_pid(preferred: int | None = None) -> int | None:
    """优先使用指定 PID；否则从正在播放音频的飞书进程里挑一个。"""
    if preferred:
        return preferred
    pids = find_feishu_pids()
    return pids[0] if pids else None


def _parse_wav(path: Path) -> tuple[int, int, int, int, bytes]:
    """返回 (采样率, 声道数, 位深, 是否 float, PCM 字节)。"""
    data = path.read_bytes()
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("不是 WAV 文件")
    i = 12
    fmt = None
    pcm = None
    while i + 8 <= len(data):
        chunk_id = data[i : i + 4]
        size = struct.unpack("<I", data[i + 4 : i + 8])[0]
        body = data[i + 8 : i + 8 + size]
        if chunk_id == b"fmt " and len(body) >= 16:
            tag, channels, rate, _, _, bits = struct.unpack("<HHIIHH", body[:16])
            is_float = tag == 3 or (
                tag == 0xFFFE and len(body) >= 40 and body[24:26] == b"\x03\x00"
            )
            fmt = (rate, channels, bits, is_float)
        elif chunk_id == b"data":
            pcm = data[i + 8 :]
            break
        i += 8 + size + (size & 1)
    if fmt is None or pcm is None:
        raise ValueError("WAV 缺少 fmt/data 段")
    return (*fmt, pcm)


def read_pac_wav(path: Path) -> np.ndarray:
    """把 DLL 录出的 WAV 转成 16k 单声道 float32。"""
    rate, channels, bits, is_float, pcm = _parse_wav(path)
    if is_float or bits == 32:
        samples = np.frombuffer(pcm[: len(pcm) // 4 * 4], dtype=np.float32)
    elif bits == 16:
        samples = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype=np.int16).astype(np.float32) / 32768.0
    else:
        raise ValueError(f"不支持的位深：{bits}")
    if channels > 1:
        samples = samples[: len(samples) // channels * channels].reshape(-1, channels).mean(axis=1)
    if rate != TARGET_SR:
        samples = Resampler(rate, TARGET_SR).process(samples)
    return np.ascontiguousarray(samples, dtype=np.float32)


class ProcessLoopbackCapture:
    """分段捕获指定进程音频，按内部块回调（接口与 AudioCapture 一致）。"""

    def __init__(
        self,
        pid: int | None,
        on_block: Callable[[np.ndarray], None],
        on_error: Callable[[str], None] | None = None,
        on_status: Callable[[str], None] | None = None,
        chunk_seconds: float = 2.5,
        channel_name: str = "远端",
    ):
        self.pid = pid
        self.on_block = on_block
        self.on_error = on_error or (lambda msg: None)
        self.on_status = on_status or (lambda msg: None)
        self.chunk_seconds = max(1.0, float(chunk_seconds))
        self.channel_name = channel_name
        self.samplerate = TARGET_SR

        self._running = False
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._tmp = Path(tempfile.gettempdir()) / f"asr_pac_{os.getpid()}_{id(self)}.wav"
        self._last_error_at = 0.0

    def start(self) -> None:
        if self._running:
            return
        if ProcessAudioCapture is None:
            raise RuntimeError("未安装 process-audio-capture，请执行 pip install process-audio-capture")
        if not is_supported():
            raise RuntimeError("当前 Windows 版本不支持进程环回（需要 Win10 2004+ / Win11）")
        self._running = True
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="process-loopback", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        try:
            self._tmp.unlink()
        except OSError:
            pass

    # ---------------- 内部 ----------------

    def _report_error(self, msg: str) -> None:
        now = time.monotonic()
        if now - self._last_error_at >= 20:
            self._last_error_at = now
            self.on_error(msg)

    def _capture_chunk(self, pid: int) -> np.ndarray:
        try:
            self._tmp.unlink()
        except OSError:
            pass
        cap = ProcessAudioCapture(
            pid=pid, output_path=str(self._tmp), mode=PacCaptureMode.INCLUDE
        )
        started = False
        try:
            cap.start()
            started = True
            self._stop.wait(self.chunk_seconds)
        finally:
            if started:
                try:
                    cap.stop()
                except Exception:
                    pass
        if not self._tmp.exists():
            return np.zeros(0, dtype=np.float32)
        return read_pac_wav(self._tmp)

    def _candidates(self) -> list[int]:
        """候选进程：显式 PID 优先；否则所有正在播放音频的飞书进程。"""
        if self.pid:
            return [self.pid]
        procs = [
            p
            for p in enumerate_audio_processes()
            if any(h in (p.name or "").lower() for h in FEISHU_HINTS)
        ]

        def score(p) -> int:
            title = (getattr(p, "window_title", "") or "").lower()
            return 0 if any(k in title for k in ("会议", "meeting", "视频", "video")) else 1

        procs.sort(key=score)
        return [p.pid for p in procs]

    def _loop(self) -> None:
        silent_chunks = 0
        cycles = 0
        announced = None
        candidates: list[int] = []
        pid: int | None = None

        while self._running:
            if cycles % 8 == 0 or not candidates or pid not in candidates:
                candidates = self._candidates()
                if pid not in candidates:
                    pid = candidates[0] if candidates else None

            if pid is None:
                if announced != "waiting":
                    announced = "waiting"
                    self.on_status("等待飞书音频（请先进入会议并让飞书有声音输出）")
                self._stop.wait(2.0)
                continue

            if announced != pid:
                announced = pid
                extra = ""
                if not self.pid and len(candidates) > 1:
                    extra = f"（{len(candidates)} 个候选进程，静音时自动切换）"
                self.on_status(f"进程环回：飞书 PID {pid}{extra}")

            try:
                pcm = self._capture_chunk(pid)
            except Exception as exc:
                self._report_error(f"进程环回采集失败：{exc}")
                self._stop.wait(1.0)
                continue

            cycles += 1
            if len(pcm):
                rms = float(np.sqrt(np.mean(np.square(pcm))))
                if rms < 1e-4:
                    silent_chunks += 1
                else:
                    silent_chunks = 0
                step = int(0.1 * TARGET_SR)
                for i in range(0, len(pcm), step):
                    if not self._running:
                        break
                    self.on_block(pcm[i : i + step])

            # 连续静音：可能选错了飞书渲染进程，轮换到下一个候选
            if silent_chunks >= 4 and not self.pid and len(candidates) > 1:
                silent_chunks = 0
                try:
                    idx = candidates.index(pid)
                except ValueError:
                    idx = -1
                pid = candidates[(idx + 1) % len(candidates)]
                announced = None
                self._stop.wait(0.5)
