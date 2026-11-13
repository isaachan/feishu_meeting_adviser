"""5.4 live check: run the real Zhipu GLM-ASR on a captured wav.

Usage: asr_live_check.py <wav> [max_segments]
Feeds the wav through the VAD segmenter and transcribes up to N segments
with the configured API key; prints text and latency per segment.
"""
import array
import sys
import time
import wave
from pathlib import Path

from local_meeting_recorder.audio import AudioFrame
from local_meeting_recorder.stt import VadSegmenter, ZhipuASR
from local_meeting_recorder.stt.base import TranscriberError

RATE = 16000


def main() -> int:
    wav_path = Path(sys.argv[1])
    max_segments = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    with wave.open(str(wav_path), 'rb') as wf:
        assert wf.getframerate() == RATE and wf.getnchannels() == 1
        raw = wf.readframes(wf.getnframes())
    samples = array.array('h', raw)
    pcm = array.array('f', (v / 32766.0 for v in samples)).tobytes()

    segmenter = VadSegmenter('mic')
    segments = []
    chunk = 3200  # 200ms frames
    for i in range(0, len(pcm) // 4, chunk):
        piece = pcm[i * 4:(i + chunk) * 4]
        segments += segmenter.feed(
            AudioFrame('mic', 0, i, RATE, 1, piece, time.monotonic()))
    segments += segmenter.flush()
    print(f'{len(segments)} segment(s) from {wav_path.name}')
    asr = ZhipuASR()
    for segment in segments[:max_segments]:
        result = asr.transcribe(
            segment.pcm,
            prompt=None,
            request_id=f'lmr-{segment.index:04d}-livecheck')
        print(f'  seg {segment.index} [{segment.duration_ms}ms '
              f'{segment.speech_ms}ms speech] -> {result.latency_ms}ms: '
              f'{result.text!r}')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except TranscriberError as exc:
        print(f'TranscriberError[{exc.reason}]: {exc}', file=sys.stderr)
        raise SystemExit(2)
