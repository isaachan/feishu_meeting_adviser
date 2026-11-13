"""ScreenCaptureKit loopback source (macOS): a Swift helper subprocess.

The helper (sck_capture.swift, shipped as package data) is compiled once
with swiftc into a cache directory and streams interleaved float32 PCM on
stdout with JSON status lines on stderr. ``LOCAL_MEETING_RECORDER_SCK_BIN``
points at a prebuilt helper binary (used by the packaged app).

Helper protocol (stderr JSON events):
  target   -> matched applications
  started  -> stream live (audio-only | video-fallback)
  format   -> actual rate/channels of the PCM on stdout
  error    -> fatal message
  stopped  -> terminal reason (stop-request | stream-error | no-target | start-failed)
stdin: the line "stop" (or EOF) requests graceful shutdown.
"""
from collections.abc import Callable
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time

from .frames import AudioFrame, SourceEvent
from .sources import AudioSource

Sink = Callable[[object], None]

_START_TIMEOUT_S = 20.0
_STOP_TIMEOUT_S = 8.0
_READ_CHUNK = 65536


class SourceStartError(RuntimeError):
    """The helper failed to reach a live capture stream."""


def helper_source_path() -> Path:
    return Path(__file__).with_name('sck_capture.swift')


def ensure_helper_binary(build_dir: Path | None = None) -> Path:
    """Return a compiled helper binary, building it if needed."""
    override = os.environ.get('LOCAL_MEETING_RECORDER_SCK_BIN')
    if override:
        path = Path(override)
        if not path.is_file():
            raise SourceStartError(f'SCK helper binary missing: {path}')
        return path
    source = helper_source_path()
    if not source.is_file():
        raise SourceStartError(f'SCK helper source missing: {source}')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:12]
    cache = (build_dir or Path(tempfile.gettempdir()) / 'local-meeting-recorder-sck')
    cache.mkdir(parents=True, exist_ok=True)
    binary = cache / f'sck_capture-{digest}'
    if binary.is_file():
        return binary
    swiftc = shutil.which('swiftc')
    if swiftc is None:
        raise SourceStartError(
            'swiftc not found; install Xcode Command Line Tools or set '
            'LOCAL_MEETING_RECORDER_SCK_BIN to a prebuilt helper binary')
    tmp = binary.with_suffix('.tmp')
    compiled = subprocess.run(
        [swiftc, '-O', '-parse-as-library', str(source), '-o', str(tmp)],
        capture_output=True, text=True, timeout=300)
    if compiled.returncode != 0:
        raise SourceStartError(f'swiftc failed:\n{compiled.stderr}')
    tmp.replace(binary)
    return binary


class SwiftLoopbackSource(AudioSource):
    """Far-end audio: Feishu-related app audio via ScreenCaptureKit.

    The SCK content filter is built once per helper start from the apps that
    exist at that moment. 飞书会议 is a separate app ("Feishu Meetings",
    com.electron.lark.iron) that appears only once a meeting window opens —
    a capture started before joining therefore filters the meeting audio out
    and delivers bit-perfect silence (2026-11-13 real-meeting finding). The
    silent-restart watchdog below rebuilds the filter automatically: real
    room audio always carries a noise floor, so N seconds of exactly-zero
    samples means the filter missed the app, and the helper is respawned.
    """

    def __init__(self, source_id: str = 'loopback', *, rate: int = 16000,
                 channels: int = 1, app_filter: list[str] | None = None,
                 display_wide: bool = False, build_dir: Path | None = None,
                 binary: Path | None = None, clock=time.monotonic,
                 silent_restart_s: float = 15.0, max_silent_restarts: int = 6):
        self.source_id = source_id
        self.rate = rate
        self.channels = channels
        self.app_filter = app_filter or ['com.electron.lark', 'lark', 'feishu', '飞书']
        self.display_wide = display_wide
        self._build_dir = build_dir
        self._binary = binary
        self._clock = clock
        self.silent_restart_s = silent_restart_s
        self.max_silent_restarts = max_silent_restarts
        self._sink: Sink | None = None
        self._proc: subprocess.Popen | None = None
        self._threads: list[threading.Thread] = []
        self._seq = 0
        self._offset = 0
        self._offset_lock = threading.Lock()
        self._live = threading.Event()
        self._format: dict | None = None
        self._stop_reason: str | None = None
        self._paused = False
        self._last_sound_at: float | None = None
        self._silent_restarts = 0
        self._watchdog_stop = threading.Event()

    def start(self, sink: Sink) -> None:
        self._sink = sink
        self._spawn()
        if not self._live.wait(_START_TIMEOUT_S):
            self._kill()
            raise SourceStartError(
                f'SCK helper did not start a stream: {self._stop_reason or "timeout"}')
        if self._format and (self._format.get('rate') != self.rate
                             or self._format.get('channels') != self.channels):
            self._kill()
            raise SourceStartError(
                f'SCK helper format mismatch: {self._format} '
                f'!= {self.rate}Hz/{self.channels}ch')
        watchdog = threading.Thread(target=self._silent_watchdog, daemon=True,
                                    name=f'{self.source_id}-watchdog')
        watchdog.start()
        self._threads.append(watchdog)

    def _spawn(self) -> None:
        assert self._sink is not None
        binary = self._binary or ensure_helper_binary(self._build_dir)
        args = [str(binary), '--rate', str(self.rate),
                '--channels', str(self.channels)]
        for app in self.app_filter:
            args += ['--app', app]
        if self.display_wide:
            args.append('--display')
        self._proc = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)
        self._live.clear()
        self._stop_reason = None
        self._last_sound_at = self._clock()
        self._threads = [
            threading.Thread(target=self._read_pcm, daemon=True,
                             name=f'{self.source_id}-pcm'),
            threading.Thread(target=self._read_status, daemon=True,
                             name=f'{self.source_id}-status'),
        ]
        for thread in self._threads:
            thread.start()

    def _read_pcm(self) -> None:
        proc, sink = self._proc, self._sink
        assert proc is not None and sink is not None and proc.stdout is not None
        while True:
            chunk = os.read(proc.stdout.fileno(), _READ_CHUNK)
            if not chunk:
                break
            if chunk.count(0) != len(chunk):
                self._last_sound_at = self._clock()  # any nonzero byte = signal
            with self._offset_lock:
                self._seq += 1
                seq, offset = self._seq, self._offset
                self._offset += len(chunk) // 4 // self.channels
            sink(AudioFrame(self.source_id, seq, offset, self.rate,
                            self.channels, chunk, self._clock()))
        self._live.clear()

    def _silent_watchdog(self) -> None:
        interval = min(1.0, self.silent_restart_s / 3)
        while not self._watchdog_stop.wait(interval):
            if self._paused or not self._live.is_set() or self._sink is None:
                continue
            silent_for = self._clock() - (self._last_sound_at or self._clock())
            if silent_for < self.silent_restart_s:
                continue
            if self._silent_restarts >= self.max_silent_restarts:
                continue  # capped: stop churning, keep delivering (silence)
            self._silent_restarts += 1
            sink = self._sink
            sink(SourceEvent(self.source_id, 'silent_restart', self._clock(),
                             {'attempt': self._silent_restarts,
                              'silent_s': round(silent_for, 1)}))
            sink(SourceEvent(self.source_id, 'gap_begin', self._clock()))
            self._kill()
            try:
                self._spawn()
            except (SourceStartError, OSError, FileNotFoundError):
                sink(SourceEvent(self.source_id, 'device_error', self._clock(),
                                 {'message': 'silent restart: helper respawn failed'}))
                return
            if not self._live.wait(_START_TIMEOUT_S):
                self._kill()
                sink(SourceEvent(self.source_id, 'device_error', self._clock(),
                                 {'message': 'silent restart: helper start timeout'}))
                return
            sink(SourceEvent(self.source_id, 'gap_end', self._clock()))

    def _read_status(self) -> None:
        proc, sink = self._proc, self._sink
        assert proc is not None and sink is not None and proc.stderr is not None
        for line in proc.stderr:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind = event.get('event')
            if kind == 'started':
                self._live.set()
                sink(SourceEvent(self.source_id, 'helper_started',
                                 self._clock(), {'mode': event.get('mode')}))
            elif kind == 'format':
                self._format = {'rate': event.get('rate'),
                                'channels': event.get('channels')}
                self._live.set()
            elif kind == 'target':
                sink(SourceEvent(self.source_id, 'target_apps',
                                 self._clock(), {'apps': event.get('apps')}))
            elif kind == 'error':
                self._stop_reason = event.get('message')
                sink(SourceEvent(self.source_id, 'device_error',
                                 self._clock(), {'message': event.get('message')}))
            elif kind == 'stopped':
                self._stop_reason = self._stop_reason or event.get('reason')
        self._live.clear()

    def pause(self) -> None:
        if self._paused or self._proc is None or self._sink is None:
            return
        self._paused = True
        self._sink(SourceEvent(self.source_id, 'gap_begin', self._clock()))
        self._kill()
        self._sink(SourceEvent(self.source_id, 'source_paused', self._clock()))

    def resume(self) -> None:
        if not self._paused or self._sink is None:
            return
        self._paused = False
        self._spawn()
        if not self._live.wait(_START_TIMEOUT_S):
            self._kill()
            self._sink(SourceEvent(self.source_id, 'device_error', self._clock(),
                                   {'message': 'resume failed: helper restart timeout'}))
            return
        self._sink(SourceEvent(self.source_id, 'gap_end', self._clock()))

    def stop(self) -> None:
        self._watchdog_stop.set()
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None and proc.stdin is not None:
            try:
                proc.stdin.write(b'stop\n')
                proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass
        try:
            proc.wait(timeout=_STOP_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
        self._join_threads()
        self._proc = None
        if self._sink is not None:
            self._sink(SourceEvent(self.source_id, 'source_stopped', self._clock(),
                                   {'reason': self._stop_reason or 'stop-request'}))

    def _kill(self) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        self._join_threads()
        self._proc = None

    def _join_threads(self) -> None:
        current = threading.current_thread()
        for thread in self._threads:
            if thread is not current:  # the watchdog may be the caller here
                thread.join(timeout=_STOP_TIMEOUT_S)
        self._threads = [thread for thread in self._threads if thread is not current]

    def join(self, timeout: float | None = None) -> None:
        self._join_threads()
