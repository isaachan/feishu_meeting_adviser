#!/usr/bin/env python3
"""Incrementally extract .vemedia AAC -> persistent FFmpeg -> 16k PCM -> STT.

Install: python3 -m pip install -r requirements.txt
Live: python3 scripts/stream_stt_mock.py /path/to/growing.vemedia --stt-provider local --local-model-dir models/MODEL
Offline smoke test: python3 scripts/stream_stt_mock.py material/FILE.vemedia --replay
Default mock text is a receipt, NOT speech recognition. No final MP4/moov is used.
"""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import threading
import time

from extract_vemedia_audio import adts_header
from stt_providers import MockSTT, create_stt_session

SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2  # PCM s16le mono


def ffmpeg_executable():
    exe = shutil.which('ffmpeg')
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except (ImportError, RuntimeError) as exc:
        raise RuntimeError('Install FFmpeg or run python3 -m pip install -r requirements.txt') from exc


class VemediaParser:
    """Incremental parser. Incomplete header/record stays buffered until complete."""
    def __init__(self):
        self.buffer = bytearray()
        self.header_seen = False
        self.asc = None
        self.rate = None
        self.last_pts = None
        self.frames = 0
        self.pts_discontinuities = 0

    @property
    def pending_bytes(self):
        return len(self.buffer)

    def feed(self, data):
        self.buffer.extend(data)
        result = []
        if not self.header_seen:
            if len(self.buffer) < 48:
                return result
            if self.buffer[4:8] != b'lark' or self.buffer[36:40] != b'mdat':
                raise ValueError('Unexpected .vemedia header')
            del self.buffer[:48]
            self.header_seen = True
        while len(self.buffer) >= 8:
            size = int.from_bytes(self.buffer[:4], 'big')
            tag = bytes(self.buffer[4:8])
            if size < 8 or size > 16 * 1024 * 1024 or tag not in (b'SSTR', b'SIFO', b'ES  '):
                raise ValueError(f'Invalid record tag={tag!r} size={size}')
            if len(self.buffer) < size:
                break
            record = bytes(self.buffer[:size])
            del self.buffer[:size]
            if tag == b'SIFO':
                caa = record.find(b'CAA\x00\x00\x00\x01', 8)
                if caa >= 0:
                    if caa + 15 > len(record):
                        raise ValueError('Truncated AAC configuration')
                    rate = int.from_bytes(record[caa+7:caa+11], 'big')
                    asc_len = int.from_bytes(record[caa+11:caa+15], 'big')
                    if asc_len < 2 or caa + 15 + asc_len > len(record):
                        raise ValueError('Invalid AAC configuration length')
                    asc = record[caa+15:caa+15+asc_len]
                    if self.asc is not None and (asc != self.asc or rate != self.rate):
                        raise ValueError('Mid-stream AAC format change is unsupported')
                    self.rate, self.asc = rate, asc
            elif tag == b'ES  ':
                if size < 23:
                    raise ValueError('Short ES record')
                if record[8:11] == b'\x00\x01\x01':
                    if self.asc is None:
                        raise ValueError('Audio arrived before AAC configuration')
                    cfg = int.from_bytes(self.asc[:2], 'big')
                    obj, freq, channels = (cfg >> 11) & 31, (cfg >> 7) & 15, (cfg >> 3) & 15
                    pts = int.from_bytes(record[11:19], 'big', signed=True)
                    if self.last_pts is not None and pts - self.last_pts != 1024:
                        self.pts_discontinuities += 1
                    self.last_pts = pts
                    self.frames += 1
                    payload = record[23:]
                    result.append((adts_header(len(payload), obj, freq, channels) + payload, pts))
        return result


class FFmpegDecoder:
    """One persistent AAC decoder. Writer and reader threads avoid pipe deadlock."""
    def __init__(self, executable):
        self.proc = subprocess.Popen([
            executable, '-nostdin', '-hide_banner', '-loglevel', 'error',
            '-f', 'aac', '-i', 'pipe:0', '-vn', '-ac', '1', '-ar', str(SAMPLE_RATE),
            '-f', 's16le', 'pipe:1'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0)
        self.input = queue.Queue(maxsize=256)
        self.output = queue.Queue(maxsize=64)
        self.writer_error = None
        self.writer = threading.Thread(target=self._write, daemon=True)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.writer.start()
        self.reader.start()

    def _write(self):
        try:
            while (adts := self.input.get()) is not None:
                self.proc.stdin.write(adts)
        except OSError as exc:
            self.writer_error = exc
        finally:
            try:
                self.proc.stdin.close()
            except OSError:
                pass

    def _read(self):
        try:
            while chunk := os.read(self.proc.stdout.fileno(), 4096):
                self.output.put(chunk)
        finally:
            self.output.put(None)

    def _enqueue(self, item, drain):
        while True:
            if self.writer_error:
                raise RuntimeError(f'FFmpeg input failed: {self.writer_error}')
            try:
                self.input.put(item, timeout=.05)
                return
            except queue.Full:
                drain()  # free stdout while ffmpeg is waiting to write to it
                if self.proc.poll() is not None:
                    raise RuntimeError(f'FFmpeg exited early with status {self.proc.returncode}')

    def send(self, adts, drain):
        self._enqueue(adts, drain)

    def close_input(self, drain):
        self._enqueue(None, drain)

    def finish_process(self):
        code = self.proc.wait(timeout=10)
        self.reader.join(timeout=1)
        self.proc.stdout.close()
        return code

    def terminate(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=5)
        self.proc.stdout.close()


class PCMChunker:
    def __init__(self, target_ms):
        if target_ms <= 0:
            raise ValueError('target_chunk_ms must be positive')
        # Round to a whole PCM sample; no byte/frame splitting.
        self.target_bytes = max(2, round(SAMPLE_RATE * target_ms / 1000) * BYTES_PER_SAMPLE)
        self.pending = bytearray()
        self.pending_since = None

    def feed(self, pcm):
        if pcm and not self.pending:
            self.pending_since = time.monotonic()
        self.pending.extend(pcm)
        chunks = []
        while len(self.pending) >= self.target_bytes:
            chunks.append(bytes(self.pending[:self.target_bytes]))
            del self.pending[:self.target_bytes]
        if not self.pending:
            self.pending_since = None
        return chunks

    def flush(self):
        if len(self.pending) % BYTES_PER_SAMPLE:
            raise ValueError('Unaligned PCM at end of stream')
        chunk = bytes(self.pending)
        self.pending.clear()
        self.pending_since = None
        return chunk


def event(log, kind, **kwargs):
    record = {'type': kind, 'monotonic_ns': time.monotonic_ns(), **kwargs}
    log.write(json.dumps(record, ensure_ascii=False) + '\n')
    log.flush()


def run(args, stt_factory=create_stt_session):
    if args.poll_ms <= 0 or args.missing_grace <= 0 or args.max_chunk_wait_ms <= 0:
        raise ValueError('poll-ms, missing-grace and max-chunk-wait-ms must be positive')
    chunker = PCMChunker(args.target_chunk_ms)
    outdir = args.outdir or (Path(__file__).resolve().parent.parent / 'material' /
                             f'stt_{args.stt_provider}_{datetime.now():%Y%m%d_%H%M%S_%f}')
    outdir.mkdir(parents=True, exist_ok=False)
    source = args.source.expanduser()
    parser = VemediaParser()
    stt = stt_factory(args.stt_provider, model_dir=args.local_model_dir,
                      local_threads=args.local_threads)
    decoder = None
    inode = None
    position = 0
    missing_since = None
    stop_reason = 'completed_replay' if args.replay else 'interrupted'

    with (outdir / 'events.jsonl').open('w') as log, (outdir / 'pcm_s16le_16k_mono.raw').open('wb') as pcm_out:
        event(log, 'start', source=str(source), outdir=str(outdir), target_chunk_ms=args.target_chunk_ms,
              stt_provider=args.stt_provider, replay=args.replay)
        print(f'outdir={outdir}', flush=True)
        def emit(responses):
            for response in responses:
                event(log, 'stt', **response)
                if response.get('type') == 'transcript':
                    print(f"[{response['status']}] {response['text']}", flush=True)

        def dispatch(pcm):
            pcm_out.write(pcm)
            for part in chunker.feed(pcm):
                emit(stt.send_pcm(part, SAMPLE_RATE))

        def flush_if_overdue():
            if chunker.pending_since is not None and (time.monotonic() - chunker.pending_since) * 1000 >= args.max_chunk_wait_ms:
                remainder = chunker.flush()
                emit(stt.send_pcm(remainder, SAMPLE_RATE))

        def drain(block=False):
            if decoder is None:
                return False
            while True:
                try:
                    item = decoder.output.get(timeout=10 if block else 0)
                except queue.Empty:
                    if block:
                        raise TimeoutError('Decoder did not finish in 10 seconds')
                    return False
                if item is None:
                    return True
                dispatch(item)

        try:
            stt.start()
            event(log, 'provider_ready', provider=args.stt_provider,
                  model_dir=str(args.local_model_dir) if args.local_model_dir else None)
            while True:
                try:
                    with source.open('rb') as f:
                        stat = os.fstat(f.fileno())
                        if inode is not None and inode != (stat.st_dev, stat.st_ino):
                            raise RuntimeError('Source replaced; refusing to concatenate recordings')
                        inode = (stat.st_dev, stat.st_ino)
                        if stat.st_size < position:
                            raise RuntimeError('Source truncated; refusing to concatenate recordings')
                        f.seek(position)
                        new = f.read()  # only the as-yet-unread suffix
                        position += len(new)
                except FileNotFoundError:
                    if args.replay:
                        raise
                    if inode is not None:
                        if missing_since is None:
                            missing_since = time.monotonic()
                            event(log, 'source_missing')
                        if time.monotonic() - missing_since >= args.missing_grace:
                            stop_reason = 'source_missing_timeout'
                            break
                    drain()
                    flush_if_overdue()
                    time.sleep(args.poll_ms / 1000)
                    continue
                if missing_since is not None:
                    event(log, 'source_resumed')
                    missing_since = None
                if new:
                    frames = parser.feed(new)
                    if frames and decoder is None:
                        decoder = FFmpegDecoder(args.ffmpeg or ffmpeg_executable())
                    for adts, pts in frames:
                        decoder.send(adts, drain)
                        # Drain after each send to avoid backpressure on file reads.
                        drain()
                    event(log, 'aac', count=len(frames), bytes_read=len(new),
                          total_frames=parser.frames, last_pts=parser.last_pts)
                drain()
                flush_if_overdue()
                if args.replay:
                    break
                time.sleep(args.poll_ms / 1000)
        except KeyboardInterrupt:
            stop_reason = 'interrupted'
        except Exception as exc:
            stop_reason = 'error'
            event(log, 'error', detail=str(exc))
            raise
        finally:
            if decoder is not None:
                if stop_reason == 'error':
                    decoder.terminate()  # never deliver additional audio after an error
                else:
                    try:
                        decoder.close_input(drain)
                        while not drain(block=True):
                            pass
                        if decoder.finish_process() != 0:
                            raise RuntimeError(f'FFmpeg exited with {decoder.proc.returncode}')
                    except Exception as exc:
                        stop_reason = 'decoder_error'
                        event(log, 'error', detail=str(exc))
                        decoder.terminate()
                        stt.cancel()
                        raise
            if stop_reason == 'error':
                stt.cancel()
            else:
                try:
                    remainder = chunker.flush()
                    if remainder:
                        emit(stt.send_pcm(remainder, SAMPLE_RATE))
                    emit(stt.finish())
                except Exception as exc:
                    stop_reason = 'stt_error'
                    event(log, 'error', detail=str(exc))
                    stt.cancel()
                    raise
            event(log, 'stop', reason=stop_reason, frames=parser.frames,
                  pts_discontinuities=parser.pts_discontinuities,
                  trailing_incomplete_bytes=parser.pending_bytes,
                  chunks=stt.chunks, pcm_seconds=stt.pcm_bytes/(SAMPLE_RATE * BYTES_PER_SAMPLE))
    print(f'frames={parser.frames} pcm_seconds={stt.pcm_bytes / (SAMPLE_RATE * BYTES_PER_SAMPLE):.3f} '
          f'chunks={stt.chunks} stop={stop_reason}', flush=True)
    return outdir


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('source', type=Path)
    ap.add_argument('--target-chunk-ms', type=int, default=200)
    ap.add_argument('--poll-ms', type=int, default=100)
    ap.add_argument('--max-chunk-wait-ms', type=int, default=300,
                    help='Flush even a short PCM chunk after this many ms (default: 300)')
    ap.add_argument('--missing-grace', type=float, default=10)
    ap.add_argument('--outdir', type=Path)
    ap.add_argument('--ffmpeg', help='FFmpeg executable (default: PATH or imageio-ffmpeg package)')
    ap.add_argument('--stt-provider', choices=('mock', 'local'), default='mock')
    ap.add_argument('--local-model-dir', type=Path, help='Streaming sherpa-onnx model directory')
    ap.add_argument('--local-threads', type=int, default=2, help='CPU inference threads for local STT')
    ap.add_argument('--replay', action='store_true', help='Read a static snapshot once, no live waiting')
    args = ap.parse_args()
    try:
        run(args)
    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
        ap.exit(1, f'error: {exc}\n')


if __name__ == '__main__':
    main()
