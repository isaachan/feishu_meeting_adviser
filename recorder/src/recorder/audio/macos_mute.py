"""Fail-closed Feishu meeting mute detector backed by macOS Accessibility."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import time


def ensure_helper_binary(build_dir=None):
    override = os.environ.get('LOCAL_MEETING_RECORDER_MUTE_BIN')
    if override:
        binary = Path(override)
        if not binary.is_file():
            raise RuntimeError(f'Mac mute helper binary missing: {binary}')
        return binary
    source = Path(__file__).with_suffix('.swift')
    digest = hashlib.sha256(source.read_bytes()).hexdigest()[:12]
    cache = Path(build_dir or Path(tempfile.gettempdir()) / 'local-meeting-recorder-mute')
    cache.mkdir(parents=True, exist_ok=True)
    binary = cache / f'macos_mute-{digest}'
    if not binary.is_file():
        swiftc = shutil.which('swiftc')
        if not swiftc:
            raise RuntimeError('swiftc not found for Mac mute helper')
        tmp = binary.with_suffix('.tmp')
        result = subprocess.run([swiftc, '-O', str(source), '-o', str(tmp)],
                                capture_output=True, text=True, timeout=300)
        if result.returncode:
            raise RuntimeError(f'Mac mute helper compile failed: {result.stderr}')
        tmp.replace(binary)
    return binary


class MacFeishuMuteDetector:
    """Require two matching readings; unknown, errors and stale data close the gate."""

    def __init__(self, *, stale_after=2.0, clock=time.monotonic, binary=None):
        self.stale_after = stale_after
        self.clock = clock
        self.binary = binary
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._proc = None
        self._pending = None
        self._count = 0
        self._state = {'muted': None, 'known': False, 'source': 'none',
                       'detail': '未启动', 'in_meeting': False, 'updated_at': 0.0}

    def apply(self, muted, source, detail, in_meeting):
        now = self.clock()
        with self._lock:
            if not in_meeting:
                muted = None
            key = (muted, source)
            if muted is None:
                self._pending, self._count = None, 0
                known = False
            else:
                self._count = self._count + 1 if key == self._pending else 1
                self._pending = key
                known = self._count >= 2
            self._state = {'muted': muted if known else None, 'known': known,
                           'source': source, 'detail': detail if known or muted is None else '等待状态确认',
                           'in_meeting': in_meeting, 'updated_at': now}

    def get_state(self):
        with self._lock:
            state = dict(self._state)
        if self.clock() - state['updated_at'] > self.stale_after:
            state.update(muted=None, known=False, source='stale', detail='检测状态已过期')
        return state

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name='macos-feishu-mute', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._proc is not None:
            try:
                self._proc.terminate()
            except ProcessLookupError:
                pass
        if self._thread:
            self._thread.join(timeout=2)
        self.apply(None, 'stopped', '检测已停止', False)

    def _loop(self):
        proc = None
        try:
            binary = self.binary or ensure_helper_binary()
            if self._stop.is_set():
                return
            proc = subprocess.Popen([str(binary)], stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True, bufsize=1)
            self._proc = proc
            if self._stop.is_set():
                return
            assert proc.stdout is not None
            for line in proc.stdout:
                if self._stop.is_set():
                    break
                try:
                    row = json.loads(line)
                    muted = row.get('muted')
                    if muted is not None and type(muted) is not bool:
                        raise ValueError('invalid mute reading')
                    self.apply(muted, str(row.get('source', 'unknown')),
                               str(row.get('detail', '')), bool(row.get('in_meeting', False)))
                except (ValueError, TypeError) as exc:
                    self.apply(None, 'error', str(exc), False)
        except Exception as exc:
            self.apply(None, 'error', str(exc), False)
        finally:
            if proc is not None:
                if proc.poll() is None:
                    try:
                        proc.terminate()
                    except ProcessLookupError:
                        pass
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                if proc.stdout is not None:
                    proc.stdout.close()
            self._proc = None
            if not self._stop.is_set():
                self.apply(None, 'error', '检测进程已退出', False)
