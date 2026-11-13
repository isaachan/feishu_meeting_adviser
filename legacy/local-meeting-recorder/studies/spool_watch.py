"""Live caption watcher for the recorder spool (5.6 acceptance aid).

Polls every spool segment file of a session and prints new captions as they
land, with the lag between caption wall clock and watch time.

Usage: spool_watch.py --data-root <root> --session <id> [--duration 600]
"""
import json
from pathlib import Path
import time


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('--data-root', type=Path, required=True)
    parser.add_argument('--session', required=True)
    parser.add_argument('--duration', type=float, default=600)
    args = parser.parse_args()

    spool = args.data_root / 'recorder' / 'spool' / args.session
    initial = set(spool.glob('*/*.jsonl')) if spool.exists() else set()
    offsets: dict[Path, int] = {}
    captions = 0
    deadline = time.monotonic() + args.duration
    print(f'watching {spool}')
    while time.monotonic() < deadline:
        for path in sorted(spool.glob('*/*.jsonl')):
            try:
                text = path.read_text(encoding='utf-8')
            except FileNotFoundError:
                continue
            if path not in offsets:
                # files that predate the watcher are skipped; files created
                # while watching (one event per file after age-rolling) are
                # read from the start
                offsets[path] = len(text) if path in initial else 0
            start = offsets[path]
            offsets[path] = len(text)
            for line in text[start:].splitlines():
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event.get('kind') == 'caption':
                    captions += 1
                    lag = time.time() - time.mktime(time.strptime(
                        event['wall_clock_at'][:19], '%Y-%m-%dT%H:%M:%S')) \
                        - (8 * 3600)  # UTC -> wall rough
                    source = event['utterance_id'].split('-')
                    source = source[2] if len(source) == 4 else source[1]
                    print(f"[{captions:3d}] {source:8s} "
                          f"{event['speaker_label'] or '远端/未知'} "
                          f"+{event['start_offset_ms']:6d}ms "
                          f"lag~{lag:5.1f}s: {event['text']}")
                elif event.get('kind') == 'stream_end':
                    print(f"--- stream_end (last_sequence="
                          f"{event['last_sequence']}) ---")
        time.sleep(0.4)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
