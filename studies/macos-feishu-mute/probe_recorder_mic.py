"""Manually check that recorder follows the Feishu meeting mic mute state.

Run on a Mac while joined to a Feishu meeting. This probe counts audio blocks
in memory; it does not save or upload microphone audio.
"""
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'recorder' / 'src'))

from recorder.audio.frames import AudioFrame
from recorder.audio.macos_mic import MacFeishuMicSource


def main() -> None:
    counts = [0, 0]  # all blocks, blocks containing nonzero audio samples
    lock = threading.Lock()

    def sink(item: object) -> None:
        if isinstance(item, AudioFrame):
            with lock:
                counts[0] += 1
                counts[1] += bool(any(item.data))

    source = MacFeishuMicSource()
    source.start(sink)
    try:
        for _ in range(400):
            time.sleep(1)
            state = source.detector.get_state()
            with lock:
                nonzero = counts[1]
                counts[:] = [0, 0]
            print(
                f"飞书: known={state['known']} muted={state['muted']} "
                f"检测={state['source']} 采集={source.stats()['state']} "
                f"非零音频块/秒={nonzero} 详情={state['detail']}",
                flush=True,
            )
    finally:
        source.stop()


if __name__ == '__main__':
    main()
