#!/usr/bin/env python3
"""Experiment B: fixed blind 1 kHz detector, then join known cue timestamps.

Detector thresholds below were selected on the 2026-10-09 pilot recordings;
DO NOT tune them on the next validation recording. Unmatched means unknown,
not necessarily missing audio. afplay launch is not calibrated acoustic onset.
"""
import argparse
from array import array
import json
import math
from pathlib import Path
import statistics
import subprocess
import tempfile
import wave

# Frozen for the next test: 40 ms windows, 10 ms hops, 1 kHz tone.
RATE = 16000
WINDOW = 640
STEP = 160
MIN_AMPLITUDE = 1.0  # 1 kHz Goertzel magnitude / window, PCM16 units
MIN_SPECTRAL_RATIO = 3.0  # power at 1 kHz / strongest of 750 Hz or 1250 Hz
MIN_DURATION = .02  # at least three consecutive 10 ms detections
MERGE_GAP = .06
MAX_MATCH_LAG = 8.0  # above this, do not guess which of two 10-s-apart cues it is


def goertzel(samples, freq):
    coefficient = 2 * math.cos(2 * math.pi * freq / RATE)
    a = b = 0.0
    for value in samples:
        a, b = value + coefficient * a - b, a
    return a * a + b * b - coefficient * a * b


def blind_onsets(path):
    with wave.open(str(path), 'rb') as audio:
        channels = audio.getnchannels()
        if audio.getsampwidth() != 2 or audio.getframerate() != RATE or channels not in (1, 2):
            raise ValueError('Expected 16 kHz mono/stereo 16-bit PCM WAV')
        raw = array('h')
        raw.frombytes(audio.readframes(audio.getnframes()))
    samples = ([sum(raw[i:i+channels]) / channels for i in range(0, len(raw), channels)]
               if channels == 2 else raw)
    active = []
    for p in range(0, len(samples) - WINDOW, STEP):
        chunk = samples[p:p+WINDOW]
        power = goertzel(chunk, 1000)
        neighbors = max(goertzel(chunk, 750), goertzel(chunk, 1250))
        if power > (WINDOW * MIN_AMPLITUDE) ** 2 and power > MIN_SPECTRAL_RATIO * max(neighbors, 1):
            active.append(p / RATE)
    groups = []
    for onset in active:
        if not groups or onset - groups[-1][-1] > MERGE_GAP:
            groups.append([onset])
        else:
            groups[-1].append(onset)
    return [g[0] for g in groups if g[-1] - g[0] >= MIN_DURATION - 1e-6]


def percentile95(values):
    ordered = sorted(values)
    return ordered[math.ceil(.95 * len(ordered)) - 1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('outdir', type=Path)
    root = parser.parse_args().outdir
    cues = [json.loads(x) for x in (root / 'cues.jsonl').read_text().splitlines()]
    events = [json.loads(x) for x in (root / 'events.jsonl').read_text().splitlines()]
    frames = [x for x in events if x['event'] == 'frame']
    decodes = [x for x in events if x['event'] == 'decode' and x['success']]
    configs = [x for x in events if x['event'] == 'config']
    if not frames or not configs or configs[0]['rate'] != 44100:
        raise ValueError('Expected AAC frames and 44.1 kHz audio config')
    with tempfile.TemporaryDirectory() as d:
        wav = Path(d) / 'decoded.wav'
        subprocess.run(['afconvert', str(root / 'audio.aac'), str(wav), '-f', 'WAVE',
                        '-d', 'LEI16@16000'], check=True)
        # No cue schedule or source timestamps enter the detector.
        onsets = blind_onsets(wav)
    candidates = []
    for onset in onsets:
        # ADTS->PCM can shift by roughly an AAC frame; report this as an estimate.
        index = min(len(frames)-1, max(0, int(onset * 44100 / 1024)))
        candidates.append(dict(pcm_seconds=onset, frame_number=index+1,
                               first_read_monotonic_ns=frames[index]['monotonic_ns']))
    used = set()
    rows = []
    for cue in cues:
        row = dict(number=cue['number'], playback_success=cue['success'])
        if not cue['success']:
            row['status'] = 'playback_failed'
        else:
            source = cue['launch_monotonic_ns']
            eligible = []
            for i, candidate in enumerate(candidates):
                lag = (candidate['first_read_monotonic_ns'] - source) / 1e9
                if i not in used and -.3 <= lag <= MAX_MATCH_LAG:
                    eligible.append((abs(lag - 2), i, lag))
            if not eligible:
                row['status'] = 'undetermined'  # never claim capture loss from non-detection
            else:
                _, i, lag = min(eligible)
                used.add(i)
                candidate = candidates[i]
                row.update(status='detected', pcm_seconds=candidate['pcm_seconds'],
                           frame_number=candidate['frame_number'], byte_delay_estimate_s=lag)
                checked = next((x for x in decodes if x['frames'] >= candidate['frame_number']
                                and x['monotonic_ns'] >= source), None)
                row['decode_checkpoint_delay_s'] = (
                    (checked['monotonic_ns']-source)/1e9 if checked else None)
        rows.append(row)
    result = dict(method='frozen 2026-10-09 pilot thresholds; not hardware calibrated',
                  thresholds=dict(amplitude=MIN_AMPLITUDE, spectral_ratio=MIN_SPECTRAL_RATIO,
                                  min_duration_s=MIN_DURATION, max_match_lag_s=MAX_MATCH_LAG),
                  candidates=len(candidates), unmatched_candidates=[candidates[i] for i in range(len(candidates)) if i not in used],
                  rows=rows)
    (root / 'b_results.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    detected = [r for r in rows if r['status'] == 'detected']
    for row in rows:
        print(f"cue {row['number']}: {row['status']}" +
              (f" pcm={row['pcm_seconds']:.3f}s bytes≈{row['byte_delay_estimate_s']:.3f}s "
               f"decode_checkpoint={row['decode_checkpoint_delay_s']}" if row['status'] == 'detected' else ''))
    print(f"cues={len(cues)} detected={len(detected)} undetermined={sum(r['status']=='undetermined' for r in rows)} "
          f"unmatched_candidates={len(result['unmatched_candidates'])}; details={root / 'b_results.json'}")
    if detected:
        for label, key in [('bytes', 'byte_delay_estimate_s'), ('decode checkpoint', 'decode_checkpoint_delay_s')]:
            vals = [r[key] for r in detected if r[key] is not None]
            if vals:
                print(f'{label}: n={len(vals)} median={statistics.median(vals):.3f}s '
                      f'p95={percentile95(vals):.3f}s max={max(vals):.3f}s '
                      '(matched subset only; launcher time is not acoustic onset)')


if __name__ == '__main__':
    main()
