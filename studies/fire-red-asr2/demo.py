#!/usr/bin/env python3
"""验证 ~/models/sherpa-onnx-fire-red-asr2-zh_en-int8-2026-02-26 能否用 sherpa-onnx 正常识别。

用法:
    studies/fire-red-asr2/.venv/bin/python studies/fire-red-asr2/demo.py
    studies/fire-red-asr2/.venv/bin/python studies/fire-red-asr2/demo.py a.wav b.wav
"""

import argparse
import wave
from pathlib import Path

import numpy as np
import sherpa_onnx

MODEL_DIR = Path.home() / "models" / "sherpa-onnx-fire-red-asr2-zh_en-int8-2026-02-26"


def read_wav(path: Path) -> tuple[int, np.ndarray]:
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise SystemExit(f"{path}: 仅支持单声道 16-bit PCM wav")
        sample_rate = w.getframerate()
        samples = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    return sample_rate, samples.astype(np.float32) / 32768.0


def resample_to_16k(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    if sample_rate == 16000:
        return samples
    duration = len(samples) / sample_rate
    x_old = np.linspace(0.0, duration, len(samples), endpoint=False)
    x_new = np.linspace(0.0, duration, int(duration * 16000), endpoint=False)
    return np.interp(x_new, x_old, samples).astype(np.float32)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wavs", nargs="*", type=Path, help="要识别的 wav，默认用模型自带 test_wavs")
    parser.add_argument("--model-dir", type=Path, default=MODEL_DIR)
    args = parser.parse_args()

    wavs = args.wavs or sorted((args.model_dir / "test_wavs").glob("*.wav"))
    recognizer = sherpa_onnx.OfflineRecognizer.from_fire_red_asr(
        encoder=str(args.model_dir / "encoder.int8.onnx"),
        decoder=str(args.model_dir / "decoder.int8.onnx"),
        tokens=str(args.model_dir / "tokens.txt"),
        num_threads=4,
        provider="cpu",
    )

    for wav_path in wavs:
        sample_rate, samples = read_wav(wav_path)
        stream = recognizer.create_stream()
        stream.accept_waveform(16000, resample_to_16k(samples, sample_rate))
        recognizer.decode_stream(stream)
        print(f"{wav_path.name}: {stream.result.text}")


if __name__ == "__main__":
    main()
