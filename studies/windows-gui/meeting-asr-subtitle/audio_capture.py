"""音频采集：设备枚举、采样率协商、重采样、PortAudio 输入流。"""

from __future__ import annotations

import ctypes
import re
import threading
import time
import warnings
from dataclasses import dataclass
from typing import Callable

import numpy as np
import sounddevice as sd

TARGET_SR = 16000

# 优先用 48k 采集：几乎所有声卡都支持，且 48k->16k 是整数 3:1 抽取，质量稳定
PREFERRED_SR = (48000, 44100, 32000, 16000)

# 自动选设备时的关键字（按优先级）
VIRTUAL_CABLE_HINTS = ("cable output", "vb-audio", "voicemeeter out")
LOOPBACK_HINTS = ("stereo mix", "立体声混音", "wave out mix", "loopback")


@dataclass
class InputDevice:
    id: int | str
    name: str
    hostapi: str
    channels: int
    default_samplerate: float
    kind: str = "input"  # input = 普通录音设备，loopback = 系统输出回环

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "hostapi": self.hostapi,
            "channels": self.channels,
            "default_samplerate": self.default_samplerate,
            "kind": self.kind,
        }

    @property
    def label(self) -> str:
        if self.kind == "loopback":
            return f"{self.name} [系统输出回环]"
        return f"{self.name} [{self.hostapi}]"


def list_input_devices() -> list[InputDevice]:
    """列出所有可用的输入设备（只保留有输入通道的）。"""
    devices: list[InputDevice] = []
    hostapis = sd.query_hostapis()
    for idx, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] < 1:
            continue
        api = hostapis[dev["hostapi"]]["name"] if dev["hostapi"] < len(hostapis) else "?"
        devices.append(
            InputDevice(
                id=idx,
                name=dev["name"],
                hostapi=api,
                channels=dev["max_input_channels"],
                default_samplerate=float(dev["default_samplerate"]),
            )
        )
    return devices


def list_loopback_devices() -> list[InputDevice]:
    """WASAPI 回环设备：直接抓某个扬声器的输出，不需要虚拟声卡。"""
    try:
        import soundcard as sc
    except Exception:
        return []

    devices: list[InputDevice] = []
    try:
        default_speaker = sc.default_speaker()
    except Exception:
        default_speaker = None

    try:
        speakers = sc.all_speakers()
    except Exception:
        return []

    for spk in speakers:
        devices.append(
            InputDevice(
                id=str(spk.id),
                name=spk.name,
                hostapi="WASAPI Loopback",
                channels=1,
                default_samplerate=48000.0,
                kind="loopback",
            )
        )

    if default_speaker is not None:
        for i, dev in enumerate(devices):
            if dev.name == default_speaker.name:
                devices.insert(0, devices.pop(i))
                break
    return devices


def list_all_devices() -> list[InputDevice]:
    """回环设备排前面（会议字幕最常用的就是它）。"""
    return list_loopback_devices() + list_input_devices()


def pick_default_device(devices: list[InputDevice]) -> InputDevice | None:
    """优先级：CABLE Output > 默认扬声器回环 > 立体声混音 > 默认输入。"""
    if not devices:
        return None

    for hint in VIRTUAL_CABLE_HINTS:
        for dev in devices:
            if dev.kind == "input" and hint in dev.name.lower():
                return dev

    # 回环最可靠，且不需要任何配置
    for dev in devices:
        if dev.kind == "loopback":
            return dev

    for hint in LOOPBACK_HINTS:
        for dev in devices:
            if hint in dev.name.lower():
                return dev

    try:
        default = sd.default.device[0]
        if isinstance(default, int) and default >= 0:
            for dev in devices:
                if dev.kind == "input" and dev.id == default:
                    return dev
    except Exception:
        pass
    return devices[0]


def default_input_device() -> int | None:
    """系统默认录音设备（麦克风），不含回环设备。"""
    try:
        idx = sd.default.device[0]
    except Exception:
        return None
    if not isinstance(idx, int) or idx < 0:
        return None
    try:
        info = sd.query_devices(idx)
    except Exception:
        return None
    return idx if info["max_input_channels"] > 0 else None


def describe_device(device_id: int | str | None) -> dict | None:
    """返回设备名称/hostapi；int 按索引，str 按名称查找。"""
    if device_id is None:
        return None
    try:
        apis = sd.query_hostapis()
        if isinstance(device_id, str):
            target = _norm_device_name(device_id)
            found = None
            for idx, dev in enumerate(sd.query_devices()):
                if dev["max_input_channels"] < 1:
                    continue
                name = _norm_device_name(dev["name"])
                if name and (name == target or target in name or name in target):
                    found = (idx, dev)
                    break
            if found is None:
                return None
            idx, info = found
        else:
            idx = int(device_id)
            info = sd.query_devices(idx)
        api = apis[info["hostapi"]]["name"] if info["hostapi"] < len(apis) else "?"
        return {"id": idx, "name": info["name"], "hostapi": api}
    except Exception:
        return None


def _ensure_com() -> None:
    """在工作线程里调用 soundcard 前初始化 COM。"""
    try:
        ctypes.windll.ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED
    except Exception:
        pass


def _norm_device_name(name: str) -> str:
    s = (name or "").lower()
    for token in ("麦克风", "microphone", "话筒"):
        s = s.replace(token, "")
    return re.sub(r"[\s\(\)（）\[\]【】,，.。\-_/]+", "", s)


_mic_cache = {"ts": 0.0, "mics": [], "default": None}
_mic_lock = threading.Lock()
_MIC_CACHE_SECONDS = 2.0


def _refresh_mic_cache() -> None:
    _ensure_com()
    mics = []
    default = None
    try:
        import soundcard as sc

        for m in sc.all_microphones(include_loopback=False):
            try:
                mics.append({"name": m.name, "id": str(getattr(m, "id", ""))})
            except Exception:
                pass
        try:
            m = sc.default_microphone()
            if m is not None:
                default = {"name": m.name, "id": str(getattr(m, "id", ""))}
        except Exception:
            default = None
    except Exception:
        pass
    with _mic_lock:
        _mic_cache.update(ts=time.monotonic(), mics=mics, default=default)


def current_mic_devices(force: bool = False) -> tuple[list[dict], dict | None]:
    """用 soundcard（WASAPI）重新枚举当前麦克风，避免 PortAudio 列表过期。"""
    with _mic_lock:
        fresh = force or time.monotonic() - _mic_cache["ts"] >= _MIC_CACHE_SECONDS
    if fresh:
        _refresh_mic_cache()
    with _mic_lock:
        return list(_mic_cache["mics"]), dict(_mic_cache["default"]) if _mic_cache["default"] else None


def same_device_name(a: str, b: str) -> bool:
    na = _norm_device_name(a)
    nb = _norm_device_name(b)
    return bool(na and nb and (na == nb or na in nb or nb in na))


def match_current_mic(bound: dict | None) -> dict | None:
    """在最新设备列表里按名称匹配绑定的麦克风；找不到表示已断开。"""
    if not bound:
        return None
    target = _norm_device_name(bound.get("name", ""))
    if not target:
        return None
    mics, _ = current_mic_devices()
    for m in mics:
        name = _norm_device_name(m.get("name", ""))
        if name and (target in name or name in target):
            return m
    return None


def refresh_portaudio() -> None:
    """重新初始化 PortAudio，刷新设备列表（热插拔后必须）。"""
    try:
        sd._terminate()
        sd._initialize()
    except Exception:
        pass


def resolve_input_device_by_name(name: str, prefer_hostapi: str = "") -> int | None:
    """按名称在当前 PortAudio 列表里找输入设备；优先相同 hostapi，其次 WASAPI。"""
    target = _norm_device_name(name)
    if not target:
        return None
    best = None
    best_score = -1
    try:
        apis = sd.query_hostapis()
        for idx, dev in enumerate(sd.query_devices()):
            if dev["max_input_channels"] < 1:
                continue
            dev_name = _norm_device_name(dev["name"])
            if not dev_name or not (target in dev_name or dev_name in target):
                continue
            api = apis[dev["hostapi"]]["name"] if dev["hostapi"] < len(apis) else ""
            score = 0
            if prefer_hostapi and api == prefer_hostapi:
                score = 3
            elif "WASAPI" in api:
                score = 2
            elif "MME" in api:
                score = 1
            if score > best_score:
                best, best_score = idx, score
    except Exception:
        return None
    return best


def negotiate_samplerate(device_id: int | str) -> int:
    """挑一个设备真正支持的采样率，优先 48k。"""
    candidates: list[int] = []
    try:
        native = int(sd.query_devices(device_id)["default_samplerate"])
    except Exception:
        native = 48000
    for sr in (*PREFERRED_SR, native):
        sr = int(sr)
        if sr > 0 and sr not in candidates:
            candidates.append(sr)

    for sr in candidates:
        try:
            sd.check_input_settings(device=device_id, samplerate=sr, channels=1, dtype="float32")
            return sr
        except Exception:
            continue
    raise RuntimeError(f"设备 {device_id} 不支持任何常用采样率（试过 {candidates}）")


class Resampler:
    """把任意采样率转成 16k。整数降采样走 box 低通 + 抽取，其它走线性插值。"""

    def __init__(self, src_sr: int, dst_sr: int = TARGET_SR):
        self.src_sr = int(src_sr)
        self.dst_sr = int(dst_sr)
        self.passthrough = self.src_sr == self.dst_sr
        self.ratio = self.src_sr / self.dst_sr

        ratio_int = round(self.ratio)
        self.decim = ratio_int if ratio_int > 1 and abs(self.ratio - ratio_int) < 1e-6 else 0

        self._acc = np.zeros(0, dtype=np.float32)  # 整数抽取时不够一组的余量
        self._buf = np.zeros(0, dtype=np.float32)  # 线性插值的锚点缓冲
        self._pos = 0.0

    def process(self, x: np.ndarray) -> np.ndarray:
        if self.passthrough:
            return x.astype(np.float32, copy=False)

        x = np.asarray(x, dtype=np.float32)

        if self.decim:
            data = np.concatenate((self._acc, x)) if self._acc.size else x
            n = len(data) // self.decim * self.decim
            if n == 0:
                self._acc = data.copy()
                return np.zeros(0, dtype=np.float32)
            out = data[:n].reshape(-1, self.decim).mean(axis=1).astype(np.float32)
            self._acc = data[n:].copy()
            return out

        buf = np.concatenate((self._buf, x)) if self._buf.size else x
        if len(buf) < 2:
            self._buf = buf.copy()
            return np.zeros(0, dtype=np.float32)

        n_out = int((len(buf) - 1 - self._pos) / self.ratio) + 1
        if n_out <= 0:
            self._buf = buf.copy()
            return np.zeros(0, dtype=np.float32)

        idx = self._pos + np.arange(n_out) * self.ratio
        out = np.interp(idx, np.arange(len(buf), dtype=np.float64), buf).astype(np.float32)
        self._pos = float(idx[-1]) + self.ratio

        cut = int(np.floor(self._pos)) - 1
        if cut > 0:
            buf = buf[cut:]
            self._pos -= cut
        self._buf = buf.copy()
        return out


class AudioCapture:
    """PortAudio 输入流：采集 -> 单声道 -> 16k -> 回调。"""

    def __init__(
        self,
        device_id: int | str,
        on_block: Callable[[np.ndarray], None],
        on_error: Callable[[str], None] | None = None,
        block_ms: int = 30,
    ):
        self.device_id = device_id
        self.on_block = on_block
        self.on_error = on_error or (lambda msg: None)
        self.block_ms = block_ms
        self.samplerate = 0
        self.stream: sd.InputStream | None = None
        self.resampler = Resampler(1)

    def _resolve_device(self) -> int:
        if isinstance(self.device_id, str):
            resolved = resolve_input_device_by_name(self.device_id)
            if resolved is None:
                raise RuntimeError(f"找不到音频设备：{self.device_id}")
            return resolved
        return int(self.device_id)

    def _open_stream(self, device: int) -> sd.InputStream:
        return sd.InputStream(
            device=device,
            samplerate=self.samplerate,
            channels=1,
            dtype="float32",
            blocksize=max(1, int(self.samplerate * self.block_ms / 1000)),
            callback=self._callback,
        )

    def start(self) -> None:
        device = self._resolve_device()
        self.samplerate = negotiate_samplerate(device)
        self.resampler = Resampler(self.samplerate, TARGET_SR)
        self.stream = self._open_stream(device)
        try:
            self.stream.start()
        except Exception:
            # 设备列表在解析和打开之间变化时，重新按名称解析一次
            self.stream.close()
            if not isinstance(self.device_id, str):
                raise
            device = self._resolve_device()
            self.samplerate = negotiate_samplerate(device)
            self.resampler = Resampler(self.samplerate, TARGET_SR)
            self.stream = self._open_stream(device)
            self.stream.start()

    def _callback(self, indata, frames, time_info, status) -> None:
        if status:
            self.on_error(f"音频流状态：{status}")
        try:
            mono = indata[:, 0]
            out = self.resampler.process(mono)
            if out.size:
                self.on_block(out)
        except Exception as exc:  # 回调里不能抛异常，否则 PortAudio 直接挂掉
            self.on_error(f"音频回调异常：{type(exc).__name__}: {exc}")

    def stop(self) -> None:
        stream, self.stream = self.stream, None
        if stream is not None:
            try:
                stream.stop()
            finally:
                stream.close()


def _co_initialize() -> bool:
    """soundcard 走 COM，在非主线程使用时必须自己初始化。"""
    try:
        import ctypes

        hr = ctypes.windll.ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED
        return hr in (0, 1)  # S_OK / S_FALSE
    except Exception:
        return False


def _co_uninitialize() -> None:
    try:
        import ctypes

        ctypes.windll.ole32.CoUninitialize()
    except Exception:
        pass


class LoopbackCapture:
    """WASAPI 回环采集：直接录某个扬声器的输出，不依赖虚拟声卡。"""

    def __init__(
        self,
        speaker_id: str,
        on_block: Callable[[np.ndarray], None],
        on_error: Callable[[str], None] | None = None,
        block_ms: int = 32,
        samplerate: int = 48000,
    ):
        self.speaker_id = speaker_id
        self.on_block = on_block
        self.on_error = on_error or (lambda msg: None)
        self.block_ms = block_ms
        self.samplerate = samplerate
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="loopback-capture", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        warnings.filterwarnings("ignore", message="data discontinuity in recording")
        com_ready = _co_initialize()
        try:
            import soundcard as sc

            mic = sc.get_microphone(id=self.speaker_id, include_loopback=True)
            frames = max(1, int(self.samplerate * self.block_ms / 1000))
            resampler = Resampler(self.samplerate, TARGET_SR)

            with mic.recorder(samplerate=self.samplerate, channels=1, blocksize=frames) as rec:
                while self._running:
                    data = rec.record(numframes=frames)
                    mono = data[:, 0] if data.ndim > 1 else data
                    out = resampler.process(np.asarray(mono, dtype=np.float32))
                    if out.size:
                        self.on_block(out)
        except Exception as exc:
            self.on_error(f"回环采集失败：{type(exc).__name__}: {exc}")
        finally:
            self._running = False
            if com_ready:
                _co_uninitialize()

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
