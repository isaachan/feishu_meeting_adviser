#!/usr/bin/env python3
"""Observe a live .vemedia, save readable bytes and incrementally extract AAC.

Start AFTER Feishu begins local recording; Ctrl-C BEFORE stopping recording.
Produces events.jsonl, observed.vemedia (bytes seen), audio.aac, and periodic
AAC->WAV decode checks. Does not use the final MP4 or its moov index.
"""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from extract_vemedia_audio import adts_header, u32


def log(fp, event, **fields):
    row = {"event": event, "monotonic_ns": time.monotonic_ns(),
           "wall_time": datetime.now().astimezone().isoformat(timespec="milliseconds"), **fields}
    fp.write(json.dumps(row, ensure_ascii=False) + "\n")
    fp.flush()
    if event in ("start", "change", "decode", "error", "stop"):
        print(row, flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('source', type=Path, help='Path to the actively growing .vemedia')
    ap.add_argument('--outdir', type=Path,
                    help='Output directory (default: material/monitor_<timestamp> in this project)')
    ap.add_argument('--interval', type=float, default=0.2)
    ap.add_argument('--decode-every', type=float, default=10.0,
                    help='Seconds between afconvert checks (0 disables)')
    args = ap.parse_args()
    if args.interval <= 0 or args.decode_every < 0:
        ap.error('interval must be positive and decode-every nonnegative')
    args.outdir = args.outdir or (Path(__file__).resolve().parent.parent / 'material' /
                                   f'monitor_{datetime.now():%Y%m%d_%H%M%S_%f}')
    args.outdir.mkdir(parents=True, exist_ok=False)
    source = args.source.resolve()
    last = None
    identity = None
    consumed = 48
    frames = 0
    last_pts = None
    rate = None
    asc = None
    last_decode = time.monotonic()
    # The observed file is an exact copy of the bytes once seen, assuming no rewrite.
    with (args.outdir / 'events.jsonl').open('w') as events, \
         (args.outdir / 'observed.vemedia').open('wb') as observed, \
         (args.outdir / 'audio.aac').open('wb') as audio:
        log(events, 'start', source=str(source), outdir=str(args.outdir), interval_s=args.interval)
        try:
            while True:
                try:
                    with source.open('rb') as fp:
                        st_before = os.fstat(fp.fileno())
                        data = fp.read()
                        st_after = os.fstat(fp.fileno())
                except FileNotFoundError:
                    if last is not None:
                        log(events, 'error', reason='source_disappeared')
                        break
                    time.sleep(args.interval)
                    continue
                ident = (st_after.st_dev, st_after.st_ino)
                if st_before.st_size != st_after.st_size or len(data) != st_after.st_size:
                    log(events, 'unstable_read', before=st_before.st_size, after=st_after.st_size,
                        read=len(data))
                    time.sleep(args.interval)
                    continue
                if identity is not None and identity != ident:
                    log(events, 'error', reason='file_replaced', old_identity=identity, new_identity=ident)
                    (args.outdir / 'changed.vemedia').write_bytes(data)
                    break
                if last is not None and len(data) < len(last):
                    log(events, 'error', reason='file_truncated', old_size=len(last), new_size=len(data))
                    (args.outdir / 'changed.vemedia').write_bytes(data)
                    break
                if last is not None and data[:len(last)] != last:
                    changed = next(i for i, (a, b) in enumerate(zip(data, last)) if a != b)
                    log(events, 'error', reason='old_bytes_rewritten', offset=changed,
                        old_size=len(last), new_size=len(data))
                    (args.outdir / 'changed.vemedia').write_bytes(data)
                    break
                identity = ident
                if last is None or len(data) > len(last):
                    prior = len(last) if last is not None else 0
                    observed.write(data[prior:])
                    observed.flush()
                    log(events, 'change', size=len(data), delta=len(data)-prior,
                        sha256=hashlib.sha256(data).hexdigest())
                    last = data
                    if len(data) >= 48:
                        if data[4:8] != b'lark' or data[36:40] != b'mdat':
                            log(events, 'error', reason='unexpected_header')
                            break
                        while consumed + 8 <= len(data):
                            size = u32(data, consumed)
                            tag = data[consumed+4:consumed+8]
                            if size < 8 or (tag not in (b'SSTR', b'SIFO', b'ES  ')):
                                log(events, 'error', reason='invalid_record', offset=consumed,
                                    size=size, tag=tag.hex())
                                return 1
                            if consumed + size > len(data):
                                break  # wait until this record is complete
                            if tag == b'SIFO':
                                caa = data.find(b'CAA\x00\x00\x00\x01', consumed+8, consumed+size)
                                if caa >= 0:
                                    rate = u32(data, caa+7)
                                    length = u32(data, caa+11)
                                    if length < 2 or caa+15+length > consumed+size:
                                        log(events, 'error', reason='bad_audio_config')
                                        return 1
                                    asc = data[caa+15:caa+15+length]
                                    log(events, 'config', rate=rate, asc=asc.hex())
                            elif tag == b'ES  ':
                                if size < 23:
                                    log(events, 'error', reason='short_ES', offset=consumed)
                                    return 1
                                if data[consumed+8:consumed+11] == b'\x00\x01\x01':
                                    if asc is None:
                                        log(events, 'error', reason='audio_before_config')
                                        return 1
                                    bits = int.from_bytes(asc[:2], 'big')
                                    payload = data[consumed+23:consumed+size]
                                    audio.write(adts_header(len(payload), (bits >> 11) & 31,
                                                            (bits >> 7) & 15, (bits >> 3) & 15))
                                    audio.write(payload)
                                    audio.flush()
                                    last_pts = int.from_bytes(data[consumed+11:consumed+19],
                                                              'big', signed=True)
                                    frames += 1
                                    log(events, 'frame', number=frames, offset=consumed+23,
                                        length=len(payload), pts=last_pts, pts_seconds=last_pts/rate)
                            consumed += size
                if args.decode_every and frames and time.monotonic()-last_decode >= args.decode_every:
                    # Decode all AAC available now. The resulting WAV is temporary;
                    # this check establishes actual decodability without final MP4.
                    last_decode = time.monotonic()
                    wav = args.outdir / 'decode_check.wav'
                    cmd = ['afconvert', str(args.outdir / 'audio.aac'), str(wav),
                           '-f', 'WAVE', '-d', 'LEI16@16000']
                    try:
                        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                        log(events, 'decode', success=result.returncode == 0,
                            frames=frames, last_pts=last_pts, last_pts_seconds=last_pts/rate,
                            wav_bytes=wav.stat().st_size if wav.exists() else 0,
                            stderr=result.stderr[-500:])
                    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
                        log(events, 'error', reason='decoder_failed', detail=str(exc))
                    wav.unlink(missing_ok=True)
                time.sleep(args.interval)
        except KeyboardInterrupt:
            pass
        log(events, 'stop', size=len(last) if last else 0, frames=frames,
            last_pts=last_pts, last_pts_seconds=last_pts/rate if rate and last_pts is not None else None)
    return 0


if __name__ == '__main__':
    sys.exit(main())
