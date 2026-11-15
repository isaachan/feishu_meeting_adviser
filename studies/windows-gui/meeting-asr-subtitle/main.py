"""本地会议字幕服务：飞书会议旁路音频 -> sherpa-onnx 离线转写 -> Web 实时字幕。

启动：python main.py
访问：http://127.0.0.1:8066

注意：sherpa-onnx 2.0.1 的真实 API 与网上示例差异很大，见 asr_engine.py 顶部说明。
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
import time
import wave
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from asr_engine import RecognitionSession, SherpaEngine, public_param_specs
from audio_capture import (
    TARGET_SR,
    Resampler,
    current_mic_devices,
    default_input_device,
    describe_device,
    list_all_devices,
    match_current_mic,
    pick_default_device,
    refresh_portaudio,
    resolve_input_device_by_name,
    same_device_name,
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
PORT = 8066
APP_VERSION = "v8.7"

WIN_NATIVE_HINT = (
    "Windows 原生识别（模式 A）尚未接入。它依赖 WinRT 的 "
    "Windows.Media.SpeechRecognition，且仅 Win11 24H2+ 可用，"
    "需要额外安装 winrt-Windows.Media.SpeechRecognition 等包。"
    "当前请使用 Sherpa 本地识别（模式 B）。"
)


class Hub:
    """WebSocket 客户端集合 + 跨线程消息队列。"""

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.queue: asyncio.Queue[dict] = asyncio.Queue()

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop

    def emit(self, msg: dict) -> None:
        """可从任意线程调用。"""
        loop = self.loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self.queue.put_nowait, msg)

    async def dispatcher(self) -> None:
        while True:
            msg = await self.queue.get()
            await self.broadcast(msg)

    async def broadcast(self, msg: dict) -> None:
        if not self.clients:
            return
        dead = []
        for ws in list(self.clients):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)


hub = Hub()


def _mic_mute_state() -> dict:
    try:
        import feishu_mute

        return feishu_mute.get_state()
    except Exception:
        return {"muted": None, "known": False, "detail": "不可用"}


class AppState:
    def __init__(self) -> None:
        self.engine = SherpaEngine(BASE_DIR / "models")
        self.sessions: dict[str, RecognitionSession] = {}
        self.mode: str = ""
        self.device_id: int | None = None
        self.loopback_id: str | None = None
        self.mic_device_id: int | str | None = None
        self.mic_device_info: dict | None = None
        self.device_lock = asyncio.Lock()
        self._mic_pending_since: float | None = None
        self._mic_pending_action: str | None = None
        self._mic_log_path = BASE_DIR / "mic_events.log"
        self.last_error: str = ""

    def _log_mic_event(self, action: str, **fields) -> None:
        try:
            payload = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "action": action, **fields}
            with self._mic_log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except Exception:
            pass

    def default_mic_snapshot(self) -> dict | None:
        _, default = current_mic_devices()
        return default

    def mic_snapshot(self) -> dict | None:
        if not self.mic_device_info:
            return None
        mics, _ = current_mic_devices()
        matched = match_current_mic(self.mic_device_info) if mics else None
        # 枚举失败（mics 为空）时保持原状态，避免误报“已断开”
        connected = True if not mics else matched is not None
        running = connected and any(
            s.is_alive() and s.device_id == self.mic_device_id
            for name, s in self.sessions.items()
            if name in ("local", "main")
        )
        info = dict(self.mic_device_info)
        info.update(connected=connected, running=running)
        if matched:
            info["current_name"] = matched.get("name")
        return info

    def _reset_mic_pending(self) -> None:
        self._mic_pending_since = None
        self._mic_pending_action = None

    async def _maybe_auto_switch(self, mics: list[dict], default: dict | None) -> None:
        """跟随 Windows 默认麦克风：默认设备稳定 2 秒后切换到它。

        - 默认设备变化（插入 USB/蓝牙、手改默认）→ 切换
        - 默认设备为空且枚举正常 → 停止本地通道
        - 默认设备仍指向已消失的绑定设备 → 等待 5 秒后停止
        - 枚举完全为空 → 暂不动作，避免误停
        """
        if self.mode != "process" or not self.mic_device_info:
            self._reset_mic_pending()
            return
        local = self.sessions.get("local")
        if local is None or not local.is_alive():
            self._reset_mic_pending()
            return
        if not mics and default is None:
            self._reset_mic_pending()
            return

        bound_present = bool(mics) and match_current_mic(self.mic_device_info) is not None
        default_name = (default or {}).get("name", "")
        same_as_bound = bool(default_name) and same_device_name(
            default_name, self.mic_device_info.get("name", "")
        )

        if default is None:
            action = "stop"
        elif same_as_bound and bound_present:
            self._reset_mic_pending()
            return
        elif same_as_bound and not bound_present:
            action = "stale_default"
        else:
            action = "switch"

        now = time.monotonic()
        if action != self._mic_pending_action:
            self._mic_pending_action = action
            self._mic_pending_since = now
            self._log_mic_event(
                "pending_" + action,
                bound=self.mic_device_info,
                default=default,
                mics=len(mics),
            )
            return
        threshold = 5.0 if action == "stale_default" else 2.0
        if now - (self._mic_pending_since or now) < threshold:
            return

        async with self.device_lock:
            local = self.sessions.get("local")
            if local is None or not local.is_alive():
                self._reset_mic_pending()
                return

            old_info = dict(self.mic_device_info or {})
            self.sessions.pop("local", None)
            await asyncio.to_thread(local.stop)

            if action in ("stop", "stale_default"):
                self._reset_mic_pending()
                if action == "stop":
                    self._log_mic_event("stop_no_default", bound=old_info, default=default)
                    msg = "没有可用的默认麦克风，已停止本地通道"
                else:
                    self._log_mic_event("stop_stale_default", bound=old_info, default=default)
                    msg = "绑定麦克风已断开且系统默认未更新，已停止本地通道"
                hub.emit({"type": "mic", "device": self.mic_snapshot(), "default": self.default_mic_snapshot()})
                hub.emit({"type": "status", "state": "running", "channel": "local", "msg": msg})
                return

            new_id = await asyncio.to_thread(
                resolve_input_device_by_name, default_name, old_info.get("hostapi", "")
            )
            refreshed = False
            if new_id is None:
                await asyncio.to_thread(refresh_portaudio)
                await asyncio.sleep(0.4)
                refreshed = True
                new_id = await asyncio.to_thread(
                    resolve_input_device_by_name, default_name, old_info.get("hostapi", "")
                )
            if new_id is None:
                new_id = await asyncio.to_thread(default_input_device)

            new_info = describe_device(new_id) if new_id is not None else None
            if new_id is None or not same_device_name((new_info or {}).get("name", ""), default_name):
                # 切不过去：能用旧设备就恢复旧设备，否则停止
                recovered_id = await asyncio.to_thread(
                    resolve_input_device_by_name, old_info.get("name", ""), old_info.get("hostapi", "")
                )
                self._reset_mic_pending()
                if recovered_id is not None:
                    recovered_name = old_info.get("name", "")
                    self.mic_device_id = recovered_name or recovered_id
                    self.mic_device_info = describe_device(recovered_id) or old_info
                    session = RecognitionSession(
                        self.engine, hub.emit, device_id=recovered_name or recovered_id,
                        channel="local", with_speaker=False, mute_gate=True,
                    )
                    self.sessions["local"] = session
                    session.start()
                    self._log_mic_event(
                        "switch_failed_recovered", old=old_info, default=default, recovered_id=recovered_id
                    )
                    hub.emit({"type": "mic", "device": self.mic_snapshot(), "default": self.default_mic_snapshot()})
                    hub.emit({"type": "status", "state": "running", "channel": "local",
                              "msg": "切换默认麦克风失败，已恢复原设备"})
                else:
                    self._log_mic_event("switch_failed", old=old_info, default=default)
                    hub.emit({"type": "mic", "device": self.mic_snapshot(), "default": self.default_mic_snapshot()})
                    hub.emit({"type": "status", "state": "running", "channel": "local",
                              "msg": "麦克风切换失败：找不到默认设备"})
                return

            new_info = new_info or dict(default)
            self.mic_device_id = default_name
            self.mic_device_info = new_info
            session = RecognitionSession(
                self.engine, hub.emit, device_id=default_name,
                channel="local", with_speaker=False, mute_gate=True,
            )
            self.sessions["local"] = session
            session.start()
            self._reset_mic_pending()
            self._log_mic_event(
                "switch_default", old=old_info, new=new_info, default=default,
                new_id=new_id, refreshed=refreshed,
            )
            hub.emit({"type": "mic", "device": self.mic_snapshot(), "default": self.default_mic_snapshot()})
            hub.emit({"type": "status", "state": "running", "channel": "local",
                      "msg": f"麦克风已切换到默认设备：{new_info.get('name', '?')}"})

    def running(self) -> bool:
        return any(s.is_alive() for s in self.sessions.values())

    def snapshot(self) -> dict:
        running = self.running()
        return {
            "state": "running" if running else "idle",
            "mode": self.mode,
            "version": APP_VERSION,
            "device_id": self.device_id,
            "loopback_id": self.loopback_id,
            "sentences": sum(s.sentence_count for s in self.sessions.values()),
            "channels": {
                name: {"running": s.is_alive(), "sentences": s.sentence_count}
                for name, s in self.sessions.items()
            },
            "speakers": self.engine.speaker_names() if self.engine.is_loaded else [],
            "mic_device": self.mic_snapshot(),
            "default_mic": self.default_mic_snapshot(),
            "mic_mute": _mic_mute_state(),
            "speaker_threshold": self.engine.params["speaker_threshold"],
            "params": dict(self.engine.params),
            "model": dict(self.engine.asr_info) if self.engine.is_loaded else {},
        }

    async def stop_session(self) -> None:
        async with self.device_lock:
            await self._stop_sessions_locked()

    async def _stop_sessions_locked(self) -> None:
        sessions, self.sessions = self.sessions, {}
        for session in sessions.values():
            await asyncio.to_thread(session.stop)

    async def start_session(
        self,
        mode: str,
        device_id: int | None,
        loopback_id: str | None = None,
        process_pid: int | None = None,
        with_mic: bool = True,
    ) -> None:
        if mode == "win_native":
            hub.emit({"type": "error", "msg": WIN_NATIVE_HINT})
            hub.emit({"type": "status", "state": "idle", "msg": "未启动"})
            return
        if mode not in ("sherpa", "process"):
            hub.emit({"type": "error", "msg": f"未知模式：{mode}"})
            return

        async with self.device_lock:
            await self._stop_sessions_locked()
            self._start_sessions_locked(mode, device_id, loopback_id, process_pid, with_mic)

    def _start_sessions_locked(
        self,
        mode: str,
        device_id: int | None,
        loopback_id: str | None,
        process_pid: int | None,
        with_mic: bool,
    ) -> None:
        self.mode = mode
        self.device_id = device_id
        self.loopback_id = loopback_id

        if mode == "process":
            sessions = {
                "remote": RecognitionSession(
                    self.engine, hub.emit, process_pid=process_pid, channel="remote"
                )
            }
            self.mic_device_id = None
            self.mic_device_info = None
            if with_mic:
                mic_id = default_input_device()
                if mic_id is None:
                    hub.emit({"type": "warning", "msg": "没有找到麦克风，只启用远端通道"})
                else:
                    self.mic_device_id = mic_id
                    self.mic_device_info = describe_device(mic_id) or self.default_mic_snapshot()
                    sessions["local"] = RecognitionSession(
                        self.engine,
                        hub.emit,
                        device_id=mic_id,
                        channel="local",
                        with_speaker=False,
                        mute_gate=True,
                    )
            self.sessions = sessions
        else:
            self.mic_device_id = device_id if loopback_id is None else None
            self.mic_device_info = (
                describe_device(device_id) if self.mic_device_id is not None else None
            )
            self.sessions = {
                "main": RecognitionSession(
                    self.engine,
                    hub.emit,
                    device_id=device_id,
                    loopback_id=loopback_id,
                    channel="main",
                )
            }

        for session in self.sessions.values():
            session.start()
        hub.emit({"type": "mic", "device": self.mic_snapshot(), "default": self.default_mic_snapshot()})


state = AppState()


async def mic_watch() -> None:
    """每秒检查麦克风：刷新显示；绑定设备消失超过 2 秒则自动切换/停止本地通道。"""
    last = None
    while True:
        await asyncio.sleep(1.0)
        if not state.sessions:
            state._reset_mic_pending()
            continue
        try:
            mics, default = await asyncio.to_thread(current_mic_devices, True)
            await state._maybe_auto_switch(mics, default)
            device = await asyncio.to_thread(state.mic_snapshot)
            default_now = await asyncio.to_thread(state.default_mic_snapshot)
            key = json.dumps([device, default_now], ensure_ascii=False, sort_keys=True)
            if key != last:
                last = key
                hub.emit({"type": "mic", "device": device, "default": default_now})
        except Exception:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    hub.bind(asyncio.get_running_loop())
    task = asyncio.create_task(hub.dispatcher())
    mic_task = asyncio.create_task(mic_watch())
    print(f"本地会议字幕: http://127.0.0.1:{PORT}")
    try:
        yield
    finally:
        task.cancel()
        mic_task.cancel()
        for session in list(state.sessions.values()):
            await asyncio.to_thread(session.stop)


app = FastAPI(title="本地会议字幕", lifespan=lifespan)


@app.get("/")
async def index():
    return FileResponse(
        STATIC_DIR / "index.html",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/api/devices")
async def api_devices():
    try:
        devices = list_all_devices()
    except Exception as exc:
        return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)
    suggested = pick_default_device(devices)
    return {
        "devices": [d.as_dict() for d in devices],
        "suggested": suggested.as_dict() if suggested else None,
    }


@app.get("/api/version")
async def api_version():
    return {"version": APP_VERSION}


@app.get("/api/status")
async def api_status():
    return state.snapshot()


@app.get("/api/params")
async def api_params():
    """返回所有可调参数的定义（默认值/范围/说明）和当前值。"""
    return {"specs": public_param_specs(), "values": dict(state.engine.params)}


@app.post("/api/params")
async def api_set_params(request: Request):
    """修改参数。请求体：{"name":"vad_min_silence","value":0.6} 或 {"vad_min_silence":0.6,...}"""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "请求体必须是 JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "请求体必须是 JSON 对象"}, status_code=400)

    items = [(body["name"], body.get("value"))] if "name" in body else list(body.items())
    updated = {}
    for name, raw in items:
        try:
            updated[name] = await asyncio.to_thread(state.engine.set_param, name, raw)
        except KeyError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except (TypeError, ValueError) as exc:
            return JSONResponse({"error": f"{name}: {exc}"}, status_code=400)
    return {"updated": updated, "values": dict(state.engine.params)}


def _wav_to_samples(data: bytes) -> np.ndarray:
    """只接受 16-bit PCM WAV，转成 16k 单声道 float32。"""
    with wave.open(io.BytesIO(data), "rb") as wf:
        if wf.getsampwidth() != 2:
            raise ValueError("仅支持 16-bit PCM WAV")
        sr, nchan = wf.getframerate(), wf.getnchannels()
        raw = wf.readframes(wf.getnframes())
    samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if nchan > 1:
        samples = samples.reshape(-1, nchan).mean(axis=1)
    if sr != TARGET_SR:
        samples = Resampler(sr, TARGET_SR).process(samples)
    return samples


@app.post("/api/transcribe")
async def api_transcribe(request: Request):
    """上传一整段 WAV 做离线转写。请求体为原始字节（Content-Type: audio/wav 或 application/octet-stream）。"""
    data = await request.body()
    if not data:
        return JSONResponse({"error": "请求体为空，需要 16-bit PCM WAV 原始字节"}, status_code=400)
    try:
        samples = _wav_to_samples(data)
    except Exception as exc:
        return JSONResponse({"error": f"WAV 解析失败：{exc}"}, status_code=400)
    try:
        await asyncio.to_thread(state.engine.ensure_loaded)
        result = await asyncio.to_thread(state.engine.transcribe_samples, samples)
    except Exception as exc:
        return JSONResponse({"error": f"{type(exc).__name__}: {exc}"}, status_code=500)
    return result


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    hub.clients.add(ws)
    await ws.send_json({"type": "status", "state": "idle", "msg": "已连接", **state.snapshot()})
    try:
        while True:
            data = await ws.receive_json()
            await handle_command(data)
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        hub.emit({"type": "warning", "msg": f"WebSocket 异常：{type(exc).__name__}: {exc}"})
    finally:
        hub.clients.discard(ws)


async def handle_command(data: dict) -> None:
    cmd = (data or {}).get("cmd")

    if cmd == "devices":
        try:
            devices = list_all_devices()
            suggested = pick_default_device(devices)
            await hub.broadcast(
                {
                    "type": "devices",
                    "devices": [d.as_dict() for d in devices],
                    "suggested": suggested.as_dict() if suggested else None,
                }
            )
        except Exception as exc:
            hub.emit({"type": "error", "msg": f"枚举设备失败：{exc}"})
        return

    if cmd == "start":
        mode = data.get("mode") or "sherpa"
        kind = data.get("kind") or "input"
        raw_device = data.get("device")

        if mode == "process":
            try:
                pid = int(data.get("pid")) if data.get("pid") else None
            except (TypeError, ValueError):
                pid = None
            await state.start_session(
                "process", None, None, process_pid=pid, with_mic=bool(data.get("mic", True))
            )
            return

        if raw_device is None:
            picked = pick_default_device(list_all_devices())
            if picked is None:
                hub.emit({"type": "error", "msg": "没有可用的音频设备"})
                return
            kind, raw_device = picked.kind, picked.id

        device_id: int | None = None
        loopback_id: str | None = None
        if kind == "loopback":
            loopback_id = str(raw_device)
        else:
            try:
                device_id = int(raw_device)
            except (TypeError, ValueError):
                device_id = None

        await state.start_session(mode, device_id, loopback_id)
        return

    if cmd == "stop":
        await state.stop_session()
        hub.emit({"type": "mic", "device": state.mic_snapshot(), "default": state.default_mic_snapshot()})
        hub.emit({"type": "status", "state": "idle", "msg": "已停止"})
        return

    if cmd == "clear":
        await hub.broadcast({"type": "clear"})
        return

    if cmd == "get_params":
        await hub.broadcast(
            {"type": "params", "specs": public_param_specs(), "values": dict(state.engine.params)}
        )
        return

    if cmd in ("set_param", "set_threshold"):
        name = data.get("name") or "speaker_threshold"
        try:
            value = await asyncio.to_thread(state.engine.set_param, name, data.get("value"))
        except KeyError as exc:
            hub.emit({"type": "warning", "msg": str(exc)})
            return
        except (TypeError, ValueError) as exc:
            hub.emit({"type": "warning", "msg": f"{name}: {exc}"})
            return
        await hub.broadcast(
            {"type": "params", "specs": public_param_specs(), "values": dict(state.engine.params)}
        )
        hub.emit(
            {
                "type": "status",
                "state": "running" if state.running() else "idle",
                "msg": f"{name} = {value}",
                **state.snapshot(),
            }
        )
        return

    if cmd == "reset_speakers":
        if state.running():
            hub.emit({"type": "warning", "msg": "请先停止识别，再重置说话人"})
            return
        state.engine.reset_speakers()
        hub.emit({"type": "speakers", "names": []})
        hub.emit({"type": "status", "state": "idle", "msg": "说话人已重置"})
        return

    if cmd == "status":
        hub.emit({"type": "status", "state": "idle", "msg": "", **state.snapshot()})
        return

    hub.emit({"type": "warning", "msg": f"未知命令：{cmd}"})


def _selftest(wav: str | None) -> int:
    """不经过声卡，直接把测试音频喂给整条识别管线。"""
    import json

    engine = SherpaEngine(BASE_DIR / "models")

    def emit(msg: dict) -> None:
        print(json.dumps(msg, ensure_ascii=False))

    if wav is None:
        candidates = sorted((BASE_DIR / "models").glob("sherpa-onnx-sense-voice*/test_wavs/*.wav"))
        if not candidates:
            print("找不到测试音频", file=sys.stderr)
            return 1
        wav = str(candidates[0])

    print(f"# selftest wav = {wav}")
    session = RecognitionSession(engine, emit, wav_path=wav)
    session.start()
    session.wait()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="本地会议字幕服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=PORT)
    parser.add_argument("--selftest", nargs="?", const="", default=None, metavar="WAV")
    args = parser.parse_args()

    if args.selftest is not None:
        raise SystemExit(_selftest(args.selftest or None))

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
