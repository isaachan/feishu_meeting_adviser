"""本地会议字幕引擎：SenseVoice 非流式识别 + VAD 切句 + 在线声纹聚类。

架构：
  音频采集 -> 重采样 16k -> Silero VAD 切出语音段 -> SenseVoice 离线识别 -> 声纹打标

SenseVoice（sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17）是非流式模型，
因此实时字幕不是逐字上屏，而是在一句话（静音断句）结束后整句输出。
参数由 PARAM_SPECS 统一定义，支持运行时修改；页面和 HTTP/WS 接口共用同一份。
"""

from __future__ import annotations

import os
import queue
import re
import threading
import time
import wave
from pathlib import Path
from typing import Callable

import numpy as np
import sherpa_onnx

from audio_capture import TARGET_SR, AudioCapture, LoopbackCapture, describe_device

# 飞书会议窗口消失超过该时长后，自动停止本地麦克风通道
NO_MEETING_STOP_SECONDS = 10.0

Emit = Callable[[dict], None]

# ---------------------------------------------------------------- 参数定义
# type: float / int / bool / choice
# apply: 实时 = 改完立即影响下一句；重建 VAD = 重新创建 VAD；重建模型 = 重新加载识别器
PARAM_SPECS: dict[str, dict] = {
    "speaker_threshold": {
        "label": "声纹阈值",
        "type": "float",
        "default": 0.45,
        "min": 0.30,
        "max": 0.95,
        "step": 0.01,
        "desc": "声纹相似度阈值。越低越容易合并成同一人，越高越容易拆成新说话人。",
        "apply": "实时",
    },
    "min_embed_seconds": {
        "label": "最短声纹时长(s)",
        "type": "float",
        "default": 0.6,
        "min": 0.2,
        "max": 3.0,
        "step": 0.1,
        "desc": "语音短于该时长不做声纹比对，直接沿用上一个说话人。",
        "apply": "实时",
    },
    "min_register_seconds": {
        "label": "最短注册时长(s)",
        "type": "float",
        "default": 1.0,
        "min": 0.5,
        "max": 5.0,
        "step": 0.1,
        "desc": "语音短于该时长不会注册新说话人，避免短句/噪声凭空造人。",
        "apply": "实时",
    },
    "vad_threshold": {
        "label": "VAD 阈值",
        "type": "float",
        "default": 0.5,
        "min": 0.10,
        "max": 0.90,
        "step": 0.05,
        "desc": "语音检测概率阈值。调高更保守（安静环境），调低更灵敏（小声说话）。",
        "apply": "重建 VAD",
    },
    "vad_min_silence": {
        "label": "断句静音(s)",
        "type": "float",
        "default": 0.5,
        "min": 0.2,
        "max": 3.0,
        "step": 0.1,
        "desc": "静音超过该时长就切句。越小字幕出得越快，但容易把一句话切碎。",
        "apply": "重建 VAD",
    },
    "vad_min_speech": {
        "label": "最短语音(s)",
        "type": "float",
        "default": 0.25,
        "min": 0.05,
        "max": 2.0,
        "step": 0.05,
        "desc": "短于该时长的声音片段会被丢弃，用于过滤咳嗽、键盘声等噪声。",
        "apply": "重建 VAD",
    },
    "max_segment_seconds": {
        "label": "单句上限(s)",
        "type": "float",
        "default": 30.0,
        "min": 5.0,
        "max": 60.0,
        "step": 1.0,
        "desc": "一句话最长时长，超过会强制切段输出，避免长时间不出字幕。",
        "apply": "重建 VAD",
    },
    "language": {
        "label": "识别语言",
        "type": "choice",
        "default": "auto",
        "options": ["auto", "zh", "en"],
        "desc": "auto 自动判断中/英；固定 zh 或 en 可减少短句误判。",
        "apply": "重建模型",
    },
    "zh_en_only": {
        "label": "仅中英文",
        "type": "bool",
        "default": True,
        "desc": "丢弃被判定为日/韩/粤的片段，以及含假名、谚文或纯符号的结果，避免噪声被识别成日语。",
        "apply": "实时",
    },
    "follow_feishu_mute": {
        "label": "跟随飞书静音",
        "type": "bool",
        "default": True,
        "desc": "飞书会议里自己静音时暂停本地麦克风通道；状态未知时放行，远端进程环回不受影响。",
        "apply": "实时",
    },
    "force_local_mic": {
        "label": "强制本地开麦",
        "type": "bool",
        "default": False,
        "desc": "忽略飞书静音状态，始终采集本地麦克风（调试或静音检测失效时使用）。",
        "apply": "实时",
    },
    "use_itn": {
        "label": "数字规整(ITN)",
        "type": "bool",
        "default": True,
        "desc": "开启后把「五十」转成「50」、「二零二六年」转成「2026年」。",
        "apply": "重建模型",
    },
    "num_threads": {
        "label": "识别线程数",
        "type": "int",
        "default": 2,
        "min": 1,
        "max": 8,
        "step": 1,
        "desc": "SenseVoice 推理线程数。越大越快，但更占 CPU。",
        "apply": "重建模型",
    },
}

SPEAKER_THRESHOLD_MIN = PARAM_SPECS["speaker_threshold"]["min"]
SPEAKER_THRESHOLD_MAX = PARAM_SPECS["speaker_threshold"]["max"]


def _default_params() -> dict:
    params = {name: spec["default"] for name, spec in PARAM_SPECS.items()}
    env = os.environ.get("SPEAKER_THRESHOLD")
    if env:
        try:
            params["speaker_threshold"] = float(env)
        except ValueError:
            pass
    return params


def public_param_specs() -> dict:
    """给前端/外部程序用的参数描述（去掉内部字段，补上默认值）。"""
    return {name: dict(spec) for name, spec in PARAM_SPECS.items()}


def _coerce_param(name: str, raw) -> object:
    spec = PARAM_SPECS.get(name)
    if spec is None:
        raise KeyError(f"未知参数：{name}")

    kind = spec["type"]
    if kind == "bool":
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, (int, float)):
            return bool(raw)
        text = str(raw).strip().lower()
        if text in ("1", "true", "yes", "on"):
            return True
        if text in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"{name} 需要布尔值")

    if kind == "choice":
        value = str(raw).strip().lower()
        if value not in spec["options"]:
            raise ValueError(f"{name} 只能是 {spec['options']}")
        return value

    if kind == "int":
        value = int(round(float(raw)))
        return max(spec["min"], min(spec["max"], value))

    value = round(float(raw), 3)
    return max(spec["min"], min(spec["max"], value))


_UNK_RE = re.compile(r"<\s*unk\s*>", re.IGNORECASE)
_KANA_RE = re.compile(r"[\u3040-\u30ff]")
_HANGUL_RE = re.compile(r"[\uac00-\ud7af\u1100-\u11ff]")
_TEXT_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]")
_OK_LANGS = ("<|zh|>", "<|en|>")


def clean_text(text: str) -> str:
    """去掉 <unk> 占位符，压缩空白。"""
    text = _UNK_RE.sub("", text or "")
    return re.sub(r"\s+", " ", text).strip()


def _find_sense_voice(models: Path) -> tuple[Path, Path, Path]:
    for cand in models.glob("sherpa-onnx-sense-voice*"):
        if not cand.is_dir():
            continue
        tokens = cand / "tokens.txt"
        model = cand / "model.int8.onnx"
        if not model.exists():
            model = cand / "model.onnx"
        if tokens.exists() and model.exists():
            return cand, model, tokens
    raise FileNotFoundError(
        f"在 {models} 下找不到 SenseVoice 模型目录（需要 sherpa-onnx-sense-voice-*，"
        "含 model.int8.onnx/model.onnx 和 tokens.txt）"
    )


class SherpaEngine:
    """模型加载、VAD 切句、离线识别、声纹聚类。"""

    def __init__(self, model_dir: str | Path = "models"):
        self.model_dir = Path(model_dir).resolve()
        self.params = _default_params()
        self._loaded = False
        self._load_lock = threading.Lock()
        self._decode_lock = threading.Lock()

        self.recognizer: sherpa_onnx.OfflineRecognizer | None = None
        self.vad: sherpa_onnx.VoiceActivityDetector | None = None
        self.segment_vad: sherpa_onnx.VoiceActivityDetector | None = None
        self.embedding_extractor: sherpa_onnx.SpeakerEmbeddingExtractor | None = None
        self.speaker_manager: sherpa_onnx.SpeakerEmbeddingManager | None = None
        self.asr_info: dict = {}

    # ---------------- 加载 ----------------

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    def ensure_loaded(self) -> None:
        with self._load_lock:
            if self._loaded:
                return
            model_dir, model_file, tokens = _find_sense_voice(self.model_dir)
            emb_model = self.model_dir / "3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx"
            vad_model = self.model_dir / "silero_vad.onnx"
            for path in (emb_model, vad_model):
                if not path.exists():
                    raise FileNotFoundError(f"缺少模型文件：{path}")

            t0 = time.time()
            self._build_recognizer(model_file, tokens)
            self._build_vads(vad_model)

            self.embedding_extractor = sherpa_onnx.SpeakerEmbeddingExtractor(
                sherpa_onnx.SpeakerEmbeddingExtractorConfig(
                    model=str(emb_model), num_threads=2, provider="cpu"
                )
            )
            self.speaker_manager = sherpa_onnx.SpeakerEmbeddingManager(
                self.embedding_extractor.dim
            )
            self._vad_model_path = vad_model

            self.asr_info = {
                "asr_kind": "sense_voice",
                "asr_model": model_file.name,
                "asr_dir": model_dir.name,
                "language": self.params["language"],
                "use_itn": self.params["use_itn"],
                "num_threads": self.params["num_threads"],
                "embedding_dim": self.embedding_extractor.dim,
                "load_seconds": round(time.time() - t0, 2),
            }
            self._loaded = True

    def _build_recognizer(self, model_file: Path | None = None, tokens: Path | None = None) -> None:
        if model_file is None or tokens is None:
            model_dir, model_file, tokens = _find_sense_voice(self.model_dir)
        language = self.params["language"]
        self.recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model_file),
            tokens=str(tokens),
            num_threads=int(self.params["num_threads"]),
            use_itn=bool(self.params["use_itn"]),
            language="" if language == "auto" else language,
            provider="cpu",
        )

    def _make_vad(
        self, max_speech: float, buffer_seconds: int, vad_model: Path | None = None
    ) -> sherpa_onnx.VoiceActivityDetector:
        path = vad_model or getattr(self, "_vad_model_path", None) or (self.model_dir / "silero_vad.onnx")
        silero = sherpa_onnx.SileroVadModelConfig(
            model=str(path),
            threshold=float(self.params["vad_threshold"]),
            min_silence_duration=float(self.params["vad_min_silence"]),
            min_speech_duration=float(self.params["vad_min_speech"]),
            window_size=512,
            max_speech_duration=float(max_speech),
        )
        cfg = sherpa_onnx.VadModelConfig(
            silero_vad=silero, sample_rate=TARGET_SR, num_threads=1, provider="cpu"
        )
        return sherpa_onnx.VoiceActivityDetector(cfg, buffer_size_in_seconds=buffer_seconds)

    def _build_vads(self, vad_model: Path | None = None) -> None:
        path = vad_model or getattr(self, "_vad_model_path", None) or (self.model_dir / "silero_vad.onnx")
        max_seg = float(self.params["max_segment_seconds"])
        self.vad = self._make_vad(max_seg, max(30, int(max_seg) + 10), path)
        self.segment_vad = self._make_vad(60.0, 60, path)
        self._vad_model_path = path

    # ---------------- 参数 ----------------

    def set_param(self, name: str, raw) -> object:
        value = _coerce_param(name, raw)
        spec = PARAM_SPECS[name]
        with self._decode_lock:
            self.params[name] = value
            if not self._loaded:
                return value
            if name in ("vad_threshold", "vad_min_silence", "vad_min_speech", "max_segment_seconds"):
                self._build_vads()
            elif name in ("language", "use_itn", "num_threads"):
                self._build_recognizer()
                self.asr_info.update(
                    language=self.params["language"],
                    use_itn=self.params["use_itn"],
                    num_threads=self.params["num_threads"],
                )
        return value

    # ---------------- 说话人 ----------------

    def set_threshold(self, value: float) -> float:
        """兼容旧接口：等价于 set_param('speaker_threshold', ...)。"""
        return float(self.set_param("speaker_threshold", value))

    def reset_speakers(self) -> None:
        if self.speaker_manager is not None:
            self.speaker_manager = sherpa_onnx.SpeakerEmbeddingManager(
                self.embedding_extractor.dim
            )

    def speaker_names(self) -> list[str]:
        if self.speaker_manager is None:
            return []
        return list(self.speaker_manager.all_speakers)

    def _speech_only(self, audio: np.ndarray) -> np.ndarray:
        """用 VAD 裁掉句子首尾静音，提升声纹质量。"""
        if self.segment_vad is None or len(audio) < 512:
            return audio
        vad = self.segment_vad
        vad.reset()
        for i in range(0, len(audio) - 511, 512):
            vad.accept_waveform(audio[i : i + 512])
        vad.flush()

        best = None
        best_len = 0
        while not vad.empty():
            seg = vad.front
            samples = seg.samples
            if len(samples) > best_len:
                best_len = len(samples)
                best = np.array(samples, dtype=np.float32)
            vad.pop()
        if best is not None and best_len >= int(0.3 * TARGET_SR):
            return best
        return audio

    def identify(
        self,
        audio: np.ndarray,
        last_speaker: str = "",
        manager: sherpa_onnx.SpeakerEmbeddingManager | None = None,
    ) -> str:
        """在线声纹聚类：命中已有说话人就复用名字，否则注册新说话人。"""
        manager = manager or self.speaker_manager
        if self.embedding_extractor is None or manager is None:
            return last_speaker or "说话人?"
        if len(audio) < int(self.params["min_embed_seconds"] * TARGET_SR):
            return last_speaker or "说话人?"

        speech = self._speech_only(audio)
        try:
            stream = self.embedding_extractor.create_stream()
            stream.accept_waveform(TARGET_SR, speech)
            stream.input_finished()
            emb = self.embedding_extractor.compute(stream)
        except Exception:
            return last_speaker or "说话人?"
        if not emb:
            return last_speaker or "说话人?"

        name = manager.search(emb, threshold=float(self.params["speaker_threshold"]))
        if name:
            return name

        # 样本太短时不急着建新说话人，避免一句话冒出一个新标签
        if len(speech) < int(self.params["min_register_seconds"] * TARGET_SR):
            return last_speaker or "说话人?"

        name = f"说话人{manager.num_speakers + 1}"
        manager.add(name, emb)
        return name

    # ---------------- 识别 ----------------

    def _decode(self, samples: np.ndarray):
        with self._decode_lock:
            recognizer = self.recognizer
            if recognizer is None:
                raise RuntimeError("识别模型尚未加载")
            stream = recognizer.create_stream()
            stream.accept_waveform(TARGET_SR, np.ascontiguousarray(samples, dtype=np.float32))
            recognizer.decode_stream(stream)
            return stream.result

    def _filter_result(self, result) -> str:
        """清理文本；开启「仅中英文」时丢弃日/韩/粤和纯噪声片段。"""
        text = clean_text(getattr(result, "text", "") or "")
        if not text:
            return ""
        if not self.params.get("zh_en_only", True):
            return text
        lang = (getattr(result, "lang", "") or "").lower()
        if lang and not any(tag in lang for tag in _OK_LANGS):
            return ""
        if _KANA_RE.search(text) or _HANGUL_RE.search(text):
            return ""
        if not _TEXT_RE.search(text):
            return ""
        return text

    def process_segment(
        self,
        samples: np.ndarray,
        last_speaker: str = "",
        manager: sherpa_onnx.SpeakerEmbeddingManager | None = None,
    ) -> tuple[str, str]:
        """识别一个语音段并打说话人标签。返回 (文本, 说话人)。"""
        with self._decode_lock:
            result = self._decode_locked(samples)
            text = self._filter_result(result)
            speaker = self.identify(samples, last_speaker, manager) if text else last_speaker
            return text, speaker

    def transcribe_segment(self, samples: np.ndarray) -> str:
        """只识别文本、不做声纹（本地麦克风通道用）。"""
        with self._decode_lock:
            return self._filter_result(self._decode_locked(samples))

    def _decode_locked(self, samples: np.ndarray):
        recognizer = self.recognizer
        if recognizer is None:
            raise RuntimeError("识别模型尚未加载")
        stream = recognizer.create_stream()
        stream.accept_waveform(TARGET_SR, np.ascontiguousarray(samples, dtype=np.float32))
        recognizer.decode_stream(stream)
        return stream.result

    def new_speaker_manager(self) -> sherpa_onnx.SpeakerEmbeddingManager:
        return sherpa_onnx.SpeakerEmbeddingManager(self.embedding_extractor.dim)

    def new_vad(self) -> sherpa_onnx.VoiceActivityDetector:
        """每个识别通道独立的 VAD，避免多通道互相 reset。"""
        max_seg = float(self.params["max_segment_seconds"])
        return self._make_vad(max_seg, max(30, int(max_seg) + 10))

    def segment_audio(self, samples: np.ndarray, vad: sherpa_onnx.VoiceActivityDetector | None = None):
        """把整段音频按 VAD 切成语音段，逐段返回 (samples, t0, t1)。"""
        vad = vad or self.vad
        if vad is None:
            raise RuntimeError("VAD 尚未加载")
        vad.reset()
        step = int(0.1 * TARGET_SR)
        fed = 0
        out: list[tuple[np.ndarray, float, float]] = []
        for i in range(0, len(samples), step):
            block = samples[i : i + step]
            vad.accept_waveform(block)
            fed += len(block)
            while not vad.empty():
                seg = vad.front
                audio = np.array(seg.samples, dtype=np.float32)
                vad.pop()
                out.append((audio, max(0.0, (fed - len(audio)) / TARGET_SR), fed / TARGET_SR))
        vad.flush()
        while not vad.empty():
            seg = vad.front
            audio = np.array(seg.samples, dtype=np.float32)
            vad.pop()
            out.append((audio, max(0.0, (fed - len(audio)) / TARGET_SR), fed / TARGET_SR))
        return out

    def transcribe_samples(self, samples: np.ndarray) -> dict:
        """整段音频离线转写（VAD 切句 + 说话人），供 HTTP 上传接口使用。"""
        self.ensure_loaded()
        manager = self.new_speaker_manager()
        # 每次请求用独立 VAD，避免和实时会话互相 reset
        vad = self._make_vad(
            float(self.params["max_segment_seconds"]),
            max(30, int(float(self.params["max_segment_seconds"])) + 10),
        )
        segments = []
        last = ""
        for audio, t0, t1 in self.segment_audio(samples, vad):
            text, last = self.process_segment(audio, last, manager)
            if text:
                segments.append(
                    {
                        "t0": round(t0, 2),
                        "t1": round(t1, 2),
                        "text": text,
                        "spk": last,
                    }
                )
        return {
            "text": "".join(s["text"] for s in segments),
            "segments": segments,
            "speakers": list(manager.all_speakers),
            "duration": round(len(samples) / TARGET_SR, 2),
            "language": self.params["language"],
        }


class RecognitionSession:
    """一次实时采集会话：采集线程 -> VAD 切句 -> 离线识别 -> 回调广播。"""

    def __init__(
        self,
        engine: SherpaEngine,
        emit: Emit,
        device_id: int | str | None = None,
        loopback_id: str | None = None,
        wav_path: str | Path | None = None,
        process_pid: int | None = None,
        channel: str = "remote",
        with_speaker: bool = True,
        mute_gate: bool = False,
    ):
        self.engine = engine
        self.emit = emit
        self.device_id = device_id
        self.loopback_id = loopback_id
        self.wav_path = Path(wav_path) if wav_path else None
        self.process_pid = process_pid
        self.channel = channel
        self.with_speaker = with_speaker
        self.mute_gate = mute_gate
        self._vad = None
        self._mute_mode = None
        self._mute_started = False
        self._no_meeting_since: float | None = None

        self.running = False
        self._thread: threading.Thread | None = None
        self._audio_q: queue.Queue[np.ndarray] = queue.Queue(maxsize=400)

        self._last_level_at = 0.0
        self._last_vad = False
        self._speaker = ""
        self._fed = 0
        self._session_start = 0.0
        self.sentence_count = 0
        self.samplerate = TARGET_SR

    # ---------------- 生命周期 ----------------

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self._thread = threading.Thread(target=self._run, name="asr-session", daemon=True)
        self._thread.start()

    def stop(self, wait: bool = True) -> None:
        self.running = False
        if wait and self._thread is not None:
            self._thread.join(timeout=8)
            self._thread = None

    def wait(self, timeout: float | None = None) -> None:
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # ---------------- 主循环 ----------------

    def _run(self) -> None:
        try:
            self.emit({"type": "status", "state": "loading", "msg": "正在加载本地模型…"})
            self.engine.ensure_loaded()
            self.emit(
                {
                    "type": "status",
                    "state": "loading",
                    "msg": f"模型已加载（{self.engine.asr_info.get('load_seconds')}s）",
                    "info": self.engine.asr_info,
                }
            )

            self._session_start = time.monotonic()
            self._fed = 0
            self._vad = self.engine.new_vad()

            if self.mute_gate and self.channel == "local":
                import feishu_mute

                feishu_mute.start()
                self._mute_started = True
                # 等首次状态就绪，避免开头几秒被误判为静音
                for _ in range(15):
                    state = feishu_mute.get_state()
                    if state.get("known"):
                        break
                    time.sleep(0.1)
                if state.get("known"):
                    self._mute_mode = "muted" if state.get("muted") else "open"
                    msg = (
                        f"飞书麦克风：{'静音' if state.get('muted') else '开麦'}"
                        f"（{state.get('source', '')}）"
                    )
                else:
                    self._mute_mode = "unknown"
                    msg = f"飞书麦克风状态未知（{state.get('detail', '')}），本地麦克风放行"
                self.emit(
                    {
                        "type": "status",
                        "state": "running",
                        "channel": "local",
                        "msg": msg,
                        "mic_mute": state,
                    }
                )

            if self.wav_path is not None:
                self._feed_file()
            else:
                use_process = self.process_pid is not None or (
                    self.channel == "remote" and self.device_id is None and self.loopback_id is None
                )
                if use_process:
                    from process_loopback import ProcessLoopbackCapture

                    capture = ProcessLoopbackCapture(
                        self.process_pid,
                        self._enqueue,
                        self._on_audio_error,
                        self._on_capture_status,
                        channel_name="远端" if self.channel == "remote" else self.channel,
                    )
                    prefix = "进程环回"
                elif self.loopback_id:
                    capture = LoopbackCapture(self.loopback_id, self._enqueue, self._on_audio_error)
                    prefix = "回环采集中"
                elif self.device_id is not None:
                    capture = AudioCapture(self.device_id, self._enqueue, self._on_audio_error)
                    prefix = "采集中"
                else:
                    raise RuntimeError("没有选择音频输入设备")

                capture.start()
                self.samplerate = capture.samplerate
                self.emit(
                    {
                        "type": "status",
                        "state": "running",
                        "msg": f"{prefix}：{capture.samplerate} Hz → 16 kHz",
                        "samplerate": capture.samplerate,
                        "device_id": self.device_id,
                        "loopback_id": self.loopback_id,
                    }
                )
                if self.device_id is not None:
                    info = describe_device(self.device_id) or {}
                    info["running"] = True
                    self.emit({"type": "mic", "device": info})
                self._consume_queue()
                capture.stop()

            self._drain_vad()
            self._finish()
        except Exception as exc:  # 把异常送前端，不要静默死掉
            self.emit({"type": "error", "msg": f"{type(exc).__name__}: {exc}"})
            self.emit({"type": "status", "state": "error", "msg": "会话异常结束"})
        finally:
            self.running = False
            if self._mute_started:
                import feishu_mute

                feishu_mute.stop()
                self._mute_started = False

    def _enqueue(self, block: np.ndarray) -> None:
        if not self.running:
            return
        try:
            self._audio_q.put_nowait(block)
        except queue.Full:
            try:
                self._audio_q.get_nowait()
                self._audio_q.put_nowait(block)
            except queue.Empty:
                pass

    def _on_audio_error(self, msg: str) -> None:
        self.emit({"type": "warning", "msg": msg})

    def _on_capture_status(self, msg: str) -> None:
        self.emit(
            {
                "type": "status",
                "state": "running",
                "msg": msg,
                "channel": self.channel,
            }
        )

    def _consume_queue(self) -> None:
        while self.running:
            try:
                block = self._audio_q.get(timeout=0.2)
            except queue.Empty:
                continue
            self._feed(block)

    def _feed_file(self) -> None:
        with wave.open(str(self.wav_path), "rb") as wf:
            sr = wf.getframerate()
            nchan = wf.getnchannels()
            raw = wf.readframes(wf.getnframes())
        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
        if nchan > 1:
            samples = samples.reshape(-1, nchan).mean(axis=1)
        if sr != TARGET_SR:
            from audio_capture import Resampler

            samples = Resampler(sr, TARGET_SR).process(samples)
        self.samplerate = sr
        self.emit(
            {
                "type": "status",
                "state": "running",
                "msg": f"回放测试音频：{self.wav_path.name}（{sr} Hz）",
                "samplerate": sr,
            }
        )

        step = int(0.032 * TARGET_SR)
        for i in range(0, len(samples), step):
            if not self.running:
                break
            self._feed(samples[i : i + step])
            time.sleep(0.032)

    # ---------------- 单块处理 ----------------

    def _mute_allows_audio(self) -> bool:
        """本地通道是否放行。

        - 强制本地开麦：始终放行
        - 关闭跟随：始终放行
        - 已知静音：拦截；已知开麦：放行
        - 状态未知：放行（避免误停）
        """
        import feishu_mute

        state = feishu_mute.get_state()

        if self.engine.params.get("force_local_mic", False):
            mode = "forced"
            allowed = True
            msg = "已手动强制本地开麦"
        elif not self.engine.params.get("follow_feishu_mute", True):
            mode = "disabled"
            allowed = True
            msg = "跟随飞书静音已关闭，本地麦克风保持开启"
        else:
            now = time.monotonic()
            if state.get("in_meeting"):
                self._no_meeting_since = None
            else:
                if self._no_meeting_since is None:
                    self._no_meeting_since = now
                elif now - self._no_meeting_since >= NO_MEETING_STOP_SECONDS:
                    if self._mute_mode != "meeting_gone":
                        self._mute_mode = "meeting_gone"
                        self.emit(
                            {
                                "type": "status",
                                "state": "running",
                                "channel": "local",
                                "msg": (
                                    f"会议窗口已消失超过 {int(NO_MEETING_STOP_SECONDS)} 秒，"
                                    "停止本地麦克风通道"
                                ),
                                "mic_mute": state,
                            }
                        )
                    self.running = False
                    return False

            if state.get("known"):
                muted = bool(state.get("muted"))
                mode = "muted" if muted else "open"
                allowed = not muted
                source = state.get("source", "")
                if muted:
                    msg = f"飞书麦克风已静音，暂停本地识别（{source}）"
                else:
                    msg = f"飞书麦克风已开启，恢复本地识别（{source}）"
            else:
                mode = "unknown"
                allowed = True
                msg = f"静音状态未知（{state.get('detail', '')}），本地麦克风放行"

        if mode != self._mute_mode:
            previous = self._mute_mode
            self._mute_mode = mode
            if mode == "muted" and self._vad is not None:
                self._vad.reset()
            # 首次进入 open 不提示，避免启动时重复消息
            if not (previous is None and mode == "open"):
                self.emit(
                    {
                        "type": "status",
                        "state": "running",
                        "channel": "local",
                        "msg": msg,
                        "mic_mute": state,
                    }
                )
        return allowed

    def _feed(self, block: np.ndarray) -> None:
        vad = self._vad
        assert vad is not None

        if self.mute_gate and self.channel == "local" and not self._mute_allows_audio():
            return

        block = np.ascontiguousarray(block, dtype=np.float32)
        now = time.monotonic()
        self._fed += len(block)
        vad.accept_waveform(block)

        while not vad.empty():
            seg = vad.front
            audio = np.array(seg.samples, dtype=np.float32)
            vad.pop()
            self._emit_segment(audio)

        speaking = bool(vad.is_speech_detected())

        # 音量 / VAD 指示（节流 100ms）
        if now - self._last_level_at >= 0.1:
            self._last_level_at = now
            rms = float(np.sqrt(np.mean(np.square(block, dtype=np.float32)))) if block.size else 0.0
            self.emit({"type": "level", "v": min(1.0, rms * 8.0)})
            if speaking != self._last_vad:
                self._last_vad = speaking
                self.emit({"type": "vad", "speaking": speaking})

    def _emit_segment(self, audio: np.ndarray) -> None:
        if len(audio) < int(self.engine.params["vad_min_speech"] * TARGET_SR):
            return
        t1 = self._fed / TARGET_SR
        t0 = max(0.0, t1 - len(audio) / TARGET_SR)
        if self.with_speaker:
            text, self._speaker = self.engine.process_segment(audio, self._speaker)
        else:
            text = self.engine.transcribe_segment(audio)
        if not text:
            return
        self.sentence_count += 1
        spk = self._speaker if self.with_speaker else "本人"
        self.emit(
            {
                "type": "final",
                "text": text,
                "spk": spk or "说话人?",
                "channel": self.channel,
                "t0": round(t0, 2),
                "t1": round(t1, 2),
                "index": self.sentence_count,
            }
        )
        if self.with_speaker:
            self.emit({"type": "speakers", "names": self.engine.speaker_names()})

    def _drain_vad(self) -> None:
        vad = self._vad
        if vad is None:
            return
        vad.flush()
        while not vad.empty():
            seg = vad.front
            audio = np.array(seg.samples, dtype=np.float32)
            vad.pop()
            self._emit_segment(audio)

    def _finish(self) -> None:
        if self.device_id is not None:
            info = describe_device(self.device_id) or {}
            info["running"] = False
            self.emit({"type": "mic", "device": info})
        self.emit(
            {
                "type": "status",
                "state": "stopped",
                "channel": self.channel,
                "msg": f"已停止，共 {self.sentence_count} 句",
                "sentences": self.sentence_count,
            }
        )
