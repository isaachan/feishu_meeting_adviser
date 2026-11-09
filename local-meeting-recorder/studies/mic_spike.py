"""5.1 spike: default microphone capture via sounddevice.

Records `--duration` seconds at 16 kHz mono, prints per-second RMS/peak,
writes a 16-bit wav. Used to validate the mic stack (sounddevice) and the
Microphone TCC flow on macOS.
"""
import argparse
import array
import math
import wave


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--duration", type=float, default=5.0)
    parser.add_argument("--rate", type=int, default=16000)
    parser.add_argument("--out", default="mic_spike.wav")
    parser.add_argument("--list-devices", action="store_true")
    args = parser.parse_args()

    import sounddevice as sd

    if args.list_devices:
        print(sd.query_devices())
        return 0

    print(f"recording {args.duration}s from default input at {args.rate} Hz ...")
    data = sd.rec(
        int(args.duration * args.rate),
        samplerate=args.rate,
        channels=1,
        dtype="float32",
    )
    sd.wait()

    samples = data[:, 0].tolist()
    per_sec = args.rate
    peak = 0.0
    for sec in range(int(args.duration)):
        chunk = samples[sec * per_sec : (sec + 1) * per_sec]
        if not chunk:
            break
        rms = math.sqrt(sum(v * v for v in chunk) / len(chunk))
        peak = max(peak, max(abs(v) for v in chunk))
        print(f"sec {sec:02d}: rms {20 * math.log10(max(rms, 1e-9)):7.1f} dB")
    print(f"peak amplitude: {peak:.3f}")

    pcm16 = array.array("h", (max(-32768, min(32767, int(v * 32766))) for v in samples))
    with wave.open(args.out, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(args.rate)
        wf.writeframes(pcm16.tobytes())
    print(f"wrote {args.out} ({len(samples)} frames)")
    if peak < 0.01:
        print("NOTE: near-silence — was the mic permission granted and did you speak?")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
