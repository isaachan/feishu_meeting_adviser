"""Measure STT accuracy (character error rate) against reference sentences.

Two modes:
  live : record N seconds from the microphone while you read the reference
         sentences aloud (pausing briefly between sentences), then VAD+ASR.
  wav  : evaluate an existing 16k mono wav (e.g. a loopback capture of the
         same sentences played through Feishu).

Usage:
  stt_eval.py live  --reference eval_sentences.txt --duration 60
  stt_eval.py wav   --reference eval_sentences.txt --path captured.wav

Output: per-sentence reference/hypothesis/CER and the overall average.
"""
import array
import sys
import time
from pathlib import Path

from local_meeting_recorder.stt import VadSegmenter, ZhipuASR
from local_meeting_recorder.stt.base import TranscriberError

RATE = 16000


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1,
                           prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def cer(reference: str, hypothesis: str, normalize: bool = False) -> float:
    ref = reference.replace(' ', '').replace('，', '').replace('。', '')
    hyp = hypothesis.replace(' ', '').replace('，', '').replace('。', '')
    if normalize:
        # ignore digit/percent normalization differences (85% vs 百分之八十五)
        drop = str.maketrans('', '', '0123456789%．.、！？!?；;：:"\'"-—…～')
        ref, hyp = ref.translate(drop), hyp.translate(drop)
    if not ref:
        return 0.0 if not hyp else 1.0
    return levenshtein(ref, hyp) / len(ref)


def transcribe_all(pcm: bytes) -> list[str]:
    segmenter = VadSegmenter('eval')
    segments = []
    for i in range(0, len(pcm) // 4, 3200):
        from local_meeting_recorder.audio import AudioFrame
        segments += segmenter.feed(
            AudioFrame('eval', 0, i, RATE, 1, pcm[i * 4:(i + 3200) * 4],
                       time.monotonic()))
    segments += segmenter.flush()
    asr = ZhipuASR()
    texts = []
    for segment in segments:
        result = asr.transcribe(segment.pcm, request_id=f'lmr-eval-{segment.index:04d}')
        texts.append(result.text)
    return texts


def record_live(duration: float) -> bytes:
    import array
    import threading
    import wave

    import sounddevice as sd

    print(f'recording {duration}s — read the sentences aloud NOW')
    data = sd.rec(int(duration * RATE), samplerate=RATE, channels=1,
                  dtype='float32')

    def progress() -> None:
        for tick in range(int(duration), 0, -5):
            time.sleep(5)
            done = int(duration) - tick + 5
            chunk = data[:done * RATE, 0]
            peak = 0.0
            if len(chunk):
                floats = array.array('f', chunk.tobytes())
                peak = max(abs(v) for v in floats[-RATE * 5:])
            print(f'  {tick:>3}s left, last-5s peak {peak:.3f}'
                  + ('  <- SPEAK LOUDER / CHECK MIC' if peak < 0.01 else ''))

    meter = threading.Thread(target=progress, daemon=True)
    meter.start()
    sd.wait()
    out = Path('eval_raw_capture.wav')
    samples = array.array('h', (int(max(-1.0, min(1.0, float(v))) * 32766)
                                for v in data[:, 0]))
    with wave.open(str(out), 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(RATE)
        wf.writeframes(samples.tobytes())
    print(f'raw capture saved to {out.resolve()}')
    return data[:, 0].tobytes()


def load_wav(path: Path) -> bytes:
    import wave

    with wave.open(str(path), 'rb') as wf:
        assert wf.getframerate() == RATE and wf.getnchannels() == 1
        raw = wf.readframes(wf.getnframes())
    samples = array.array('h', raw)
    return array.array('f', (v / 32766.0 for v in samples)).tobytes()


def evaluate(references: list[str], hypotheses: list[str],
             normalize: bool = False) -> float:
    used = [False] * len(hypotheses)
    rows = []
    pending: list[str] = []
    for ref in references:
        best_idx, best_cer = None, 2.0
        for idx, hyp in enumerate(hypotheses):
            if used[idx] or not hyp:
                continue
            score = cer(ref, hyp, normalize)
            if score < best_cer:
                best_idx, best_cer = idx, score
        if best_idx is None or best_cer > 0.9:
            pending.append(ref)
            continue
        used[best_idx] = True
        rows.append((ref, hypotheses[best_idx], best_cer))
    extra = [h for i, h in enumerate(hypotheses) if not used[i] and h]
    for ref, hyp, score in rows:
        mark = 'OK ' if score <= 0.15 else ('~~ ' if score <= 0.4 else 'BAD')
        print(f'{mark} CER {score * 100:5.1f}%  ref: {ref}')
        print(f'          hyp: {hyp}')
    for ref in pending:
        print(f'MISS      ref: {ref}')
    for hyp in extra:
        print(f'EXTRA     hyp: {hyp}')
    scores = [s for _, _, s in rows]
    overall = sum(scores) / len(scores) if scores else 1.0
    print(f'\nmatched {len(rows)}/{len(references)}  '
          f'avg CER {overall * 100:.1f}%  (extra segments: {len(extra)})')
    return overall


def main() -> int:
    args = sys.argv[1:]
    mode = args[0] if args else 'live'
    if mode not in ('live', 'wav'):
        raise SystemExit('mode must be live or wav')
    rest = args[1:]
    reference_file = Path('local-meeting-recorder/studies/eval_sentences.txt')
    wav_path = None
    duration = 60.0
    normalize = False
    i = 0
    while i < len(rest):
        if rest[i] == '--reference':
            reference_file = Path(rest[i + 1]); i += 2
        elif rest[i] == '--path':
            wav_path = Path(rest[i + 1]); i += 2
        elif rest[i] == '--duration':
            duration = float(rest[i + 1]); i += 2
        elif rest[i] == '--normalize':
            normalize = True; i += 1
        else:
            i += 1
    references = [line.strip() for line in reference_file.read_text().splitlines()
                  if line.strip() and not line.startswith('#')]
    print(f'{len(references)} reference sentences from {reference_file}')
    if mode == 'live':
        pcm = record_live(duration)
    else:
        pcm = load_wav(wav_path)
    print('transcribing ...')
    hypotheses = transcribe_all(pcm)
    print(f'{len(hypotheses)} segment(s)\n')
    evaluate(references, hypotheses, normalize)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except TranscriberError as exc:
        print(f'TranscriberError[{exc.reason}]: {exc}', file=sys.stderr)
        raise SystemExit(2)
