#!/usr/bin/env python3
"""Local .vemedia recording + streaming STT feeding `collect --captions -`.

Uses the local input pipeline (incremental .vemedia AAC extraction, resident
FFmpeg decode to 16 kHz PCM, streaming sherpa-onnx STT) and forwards each
FINAL utterance as one JSONL caption line to the meeting-listener collector; STT
partials are revisions and are never forwarded. Audio never leaves this
machine; only recognized text enters the product storage.

Run from meeting-listener with its virtualenv interpreter, e.g.:

  .venv/bin/python scripts/collect-local.py /path/to/growing.vemedia \
      --stt-provider local \
      --local-model-dir models/sherpa-onnx-x-asr-480ms-streaming-zipformer-transducer-zh-en-punct-int8-2026-06-05

After `./join <meeting>`; `./consume --watch` keeps working unchanged. The model
directory is not committed: on first local use the script downloads the official
sherpa-onnx release once (checksum-verified); `--download-model` pre-fetches it.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
from uuid import uuid4

LISTENER_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCAL_MEETING_SCRIPTS = LISTENER_ROOT / 'scripts'
MODEL_NAME = 'sherpa-onnx-x-asr-480ms-streaming-zipformer-transducer-zh-en-punct-int8-2026-06-05'
DEFAULT_MODEL_DIR = LISTENER_ROOT / 'models' / MODEL_NAME
MODEL_URL = ('https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/'
             + MODEL_NAME + '.tar.bz2')
MODEL_SHA256 = 'fa5f63d618e5a01526e275a358bb7772e403f84808a4769fba52cffd8160bf74'


def model_files_present(model_dir: Path) -> bool:
    return all((model_dir / name).is_file() for name in
               ('tokens.txt', 'encoder.int8.onnx', 'decoder.onnx', 'joiner.int8.onnx'))


def ensure_model(model_dir: Path) -> Path:
    if model_files_present(model_dir):
        return model_dir
    parent = model_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    part = parent / f'.{model_dir.name}.{os.getpid()}.part'
    digest = hashlib.sha256()
    print(f'model missing; downloading {MODEL_URL}', file=sys.stderr, flush=True)
    with urllib.request.urlopen(MODEL_URL, timeout=60) as response, part.open('wb') as out:
        total = int(response.headers.get('Content-Length') or 0)
        done = 0
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            digest.update(chunk)
            done += len(chunk)
            if total:
                print(f'  {done >> 20}/{total >> 20} MiB', end='\r', file=sys.stderr, flush=True)
        print(file=sys.stderr)
    if digest.hexdigest() != MODEL_SHA256:
        part.unlink(missing_ok=True)
        raise SystemExit('error: model checksum mismatch; partial download removed, retry later')
    staging = parent / f'.{model_dir.name}.{os.getpid()}.extract'
    if staging.exists():
        shutil.rmtree(staging)
    with tarfile.open(part, 'r:bz2') as tar:
        tar.extractall(staging, filter='data')
    part.unlink(missing_ok=True)
    extracted = staging / model_dir.name
    if not model_files_present(extracted):
        shutil.rmtree(staging, ignore_errors=True)
        raise SystemExit('error: downloaded archive lacks the expected model files')
    extracted.replace(model_dir)
    shutil.rmtree(staging, ignore_errors=True)
    print(f'model ready: {model_dir}', file=sys.stderr, flush=True)
    return model_dir


def iso_utc(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec='milliseconds')


def recording_start(source: Path, override: float | None) -> float:
    if override is not None:
        return override
    try:
        return os.stat(source).st_birthtime
    except AttributeError:
        raise SystemExit('error: no file birthtime on this platform; pass --recording-start <unix-seconds>') from None


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('vemedia', nargs='?', type=Path, help='path to the actively growing .vemedia recording')
    ap.add_argument('--stt-provider', choices=('mock', 'local'), default='local')
    ap.add_argument('--local-model-dir', type=Path, default=DEFAULT_MODEL_DIR,
                    help=f'streaming sherpa-onnx model directory (default: {DEFAULT_MODEL_DIR})')
    ap.add_argument('--download-model', action='store_true',
                    help='download the model if missing, then exit')
    ap.add_argument('--local-threads', type=int, default=2)
    ap.add_argument('--speaker', default='local', help='speaker label attached to forwarded captions')
    ap.add_argument('--poll-ms', type=int, default=100)
    ap.add_argument('--missing-grace', type=float, default=10,
                    help='stop after the source file stays missing this long (meeting end)')
    ap.add_argument('--target-chunk-ms', type=int, default=200)
    ap.add_argument('--max-chunk-wait-ms', type=int, default=300)
    ap.add_argument('--ffmpeg', help='FFmpeg executable (default: PATH or imageio-ffmpeg package)')
    ap.add_argument('--recording-start', type=float,
                    help='recording start as unix seconds (default: .vemedia file birthtime)')
    ap.add_argument('--replay', action='store_true', help='read a static snapshot once, no live waiting')
    ap.add_argument('--local-meeting-scripts', type=Path, default=DEFAULT_LOCAL_MEETING_SCRIPTS,
                    help='scripts directory providing the parser/decoder/STT modules')
    ap.add_argument('--python', type=Path, default=LISTENER_ROOT / '.venv' / 'bin' / 'python',
                    help='meeting-listener virtualenv python used to run collect')
    ap.add_argument('--config', type=Path, help='pass an explicit product config path to collect')
    ap.add_argument('--no-text', action='store_true', help='do not print caption text in the collect terminal')
    args = ap.parse_args()
    if args.poll_ms <= 0 or args.missing_grace <= 0 or args.max_chunk_wait_ms <= 0:
        ap.error('poll-ms, missing-grace and max-chunk-wait-ms must be positive')

    if args.download_model:
        ensure_model(args.local_model_dir.expanduser().resolve())
        return 0
    if not args.vemedia:
        ap.error('vemedia path required unless using --download-model')

    sys.path.insert(0, str(args.local_meeting_scripts.resolve()))
    from stream_stt_mock import FFmpegDecoder, PCMChunker, VemediaParser, ffmpeg_executable
    from stt_providers import SAMPLE_RATE, create_stt_session

    if not args.python.is_file():
        ap.error(f'meeting-listener python not found: {args.python}')
    source = args.vemedia.expanduser().resolve()
    if not source.exists():
        ap.error(f'vemedia file not found: {source}')
    base = recording_start(source, args.recording_start)
    if args.stt_provider == 'local':
        ensure_model(args.local_model_dir.expanduser().resolve())

    collect_args = [str(args.python), '-m', 'meeting_listener']
    if args.config:
        collect_args += ['--config', str(args.config.expanduser().resolve())]
    collect_args += ['collect', '--captions', '-', '--speaker', args.speaker]
    if args.no_text:
        collect_args += ['--no-text']
    proc = subprocess.Popen(collect_args, stdin=subprocess.PIPE, cwd=str(LISTENER_ROOT))

    run_id = uuid4().hex[:12]
    last_end = None
    parser = VemediaParser()
    chunker = PCMChunker(args.target_chunk_ms)
    decoder = None
    inode = None
    position = 0
    missing_since = None
    stop_reason = 'interrupted'

    def forward(responses):
        nonlocal last_end
        for response in responses:
            if response.get('type') != 'transcript':
                continue
            text = response.get('text', '').strip()
            if response.get('status') != 'final':
                if text:
                    print(f'[partial] {text}', file=sys.stderr, flush=True)
                continue
            if not text:
                continue
            end = min(base + response['audio_seconds'], time.time())
            if last_end is not None and end < last_end:
                end = last_end
            line = {'id': f'{run_id}:{response["segment"]}', 'speaker': args.speaker,
                    'text': text, 'end_time': iso_utc(end)}
            try:
                proc.stdin.write(json.dumps(line, ensure_ascii=False).encode() + b'\n')
                proc.stdin.flush()
            except (BrokenPipeError, ValueError):
                raise RuntimeError(f'collect process exited early with code {proc.poll()}') from None
            print(f'[final] {text}', file=sys.stderr, flush=True)
            last_end = end

    def dispatch(pcm):
        for part in chunker.feed(pcm):
            forward(stt.send_pcm(part, SAMPLE_RATE))

    def flush_if_overdue():
        if chunker.pending_since is not None and (time.monotonic() - chunker.pending_since) * 1000 >= args.max_chunk_wait_ms:
            dispatch(chunker.flush())

    def drain(block=False):
        if decoder is None:
            return False
        while True:
            try:
                item = decoder.output.get(timeout=10 if block else 0)
            except queue.Empty:
                if block:
                    raise TimeoutError('decoder did not finish in 10 seconds')
                return False
            if item is None:
                return True
            dispatch(item)

    stt = create_stt_session(args.stt_provider, model_dir=args.local_model_dir,
                             local_threads=args.local_threads)
    try:
        stt.start()
        print(f'local input ready: source={source} provider={args.stt_provider} '
              f'recording_start={iso_utc(base)}', file=sys.stderr, flush=True)
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
                    new = f.read()
                    position += len(new)
            except FileNotFoundError:
                if args.replay:
                    raise
                if inode is not None:
                    if missing_since is None:
                        missing_since = time.monotonic()
                    if time.monotonic() - missing_since >= args.missing_grace:
                        stop_reason = 'source_missing_timeout'
                        break
                time.sleep(args.poll_ms / 1000)
                continue
            missing_since = None
            if new:
                frames = parser.feed(new)
                if frames and decoder is None:
                    decoder = FFmpegDecoder(args.ffmpeg or ffmpeg_executable())
                for adts, _pts in frames:
                    decoder.send(adts, drain)
                    drain()
            drain()
            flush_if_overdue()
            if args.replay:
                stop_reason = 'completed_replay'
                break
            time.sleep(args.poll_ms / 1000)
    except KeyboardInterrupt:
        stop_reason = 'interrupted'
    except Exception:
        stop_reason = 'error'
        raise
    finally:
        if decoder is not None:
            if stop_reason == 'error':
                decoder.terminate()
            else:
                try:
                    decoder.close_input(drain)
                    while not drain(block=True):
                        pass
                    if decoder.finish_process() != 0:
                        raise RuntimeError(f'FFmpeg exited with {decoder.proc.returncode}')
                except Exception:
                    stop_reason = 'decoder_error'
                    decoder.terminate()
                    stt.cancel()
                    raise
        if stop_reason == 'error':
            stt.cancel()
        else:
            try:
                remainder = chunker.flush()
                if remainder:
                    dispatch(remainder)
                forward(stt.finish())
            except Exception:
                stop_reason = 'stt_error'
                stt.cancel()
                raise
        try:
            proc.stdin.close()
        except (BrokenPipeError, OSError):
            pass
        code = proc.wait()
        print(f'stop={stop_reason} frames={parser.frames} '
              f'pcm_seconds={stt.pcm_bytes / (SAMPLE_RATE * 2):.3f} collect_exit={code}',
              file=sys.stderr, flush=True)
    return code if code != 0 else (1 if stop_reason in ('error', 'decoder_error', 'stt_error') else 0)


if __name__ == '__main__':
    sys.exit(main())
