"""Capture orchestration: the recorder library driven by the GUI (Qt-free).

Mirrors recorder.__main__.run_recording semantics: per-source uplinks, a pump
thread, clean stream_end stop (or interrupt on quit), exit-code classification
(0 clean / 2 auth / 3 unrecoverable / 130 interrupt). The UI layer polls
stats() and notices completion via state -> 'stopped'.
"""
import threading
import time
from pathlib import Path

from recorder.audio import CaptureSession
from recorder.audio.frames import AudioFrame, SourceEvent
from recorder.audio.sources import AudioSource, MicSource
from recorder.uplink import SourceUplink

EXIT_OK, EXIT_ARGS, EXIT_CONN, EXIT_INTERRUPT = 0, 2, 3, 130
ARGS_KEYS = ('session', 'event', 'url', 'token')


def default_source_factory(mic_enabled: bool):
    """Real sources (mac first): SCK loopback lazily imported, optional mic."""
    from recorder.audio.sck_capture import SwiftLoopbackSource
    sources: list[AudioSource] = [SwiftLoopbackSource('loopback')]
    if mic_enabled:
        sources.append(MicSource('mic'))
    return sources


class CaptureJob:
    """One capture run for one session's recorder_command_args."""

    def __init__(self, recorder_args, *, out_root, sources=None, poll_interval=0.1):
        missing = [key for key in ARGS_KEYS if not recorder_args.get(key)]
        if missing:
            raise ValueError(f'recorder args missing: {missing}')
        self.args = dict(recorder_args)
        self.out_root = Path(out_root)
        self._sources = sources  # injectable (tests); None = real sources
        self._poll_interval = poll_interval
        self._stop = threading.Event()
        self._interrupted = threading.Event()
        self._finished = threading.Event()
        self._lock = threading.Lock()
        self._uplinks: list[SourceUplink] = []
        self._session = None
        self.state = 'idle'  # idle|running|stopping|stopped
        self.result = None

    @property
    def finished(self):
        return self._finished.is_set()

    @property
    def session_id(self):
        return self.args.get('session')

    def start(self):
        if self.state != 'idle':
            raise RuntimeError('job already started')
        sources: list[AudioSource] = \
            self._sources if self._sources is not None else \
            default_source_factory(bool(self.args.get('mic')))
        self.out_root.mkdir(parents=True, exist_ok=True)
        session = CaptureSession(sources, self.out_root)
        uplinks = []
        for source in sources:
            uplinks.append(SourceUplink(
                source.source_id, session_id=self.args['session'],
                event=self.args['event'], url=self.args['url'],
                token=self.args['token'],
                speaker=self.args.get('speaker') if source.source_id == 'mic' else None))
        self._session, self._uplinks = session, uplinks
        subscriber = session.subscribe()
        self.state = 'running'

        def pump(item):
            if not isinstance(item, (AudioFrame, SourceEvent)):
                return
            for uplink in uplinks:
                if item.source_id == uplink.source_id:
                    uplink.offer(item)

        def drain():
            while not self._stop.is_set():
                try:
                    pump(subscriber.get(timeout=0.1))
                except Exception:  # queue.Empty
                    continue

        def wait_capture_drained(timeout=2.0):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if (subscriber.empty()
                        and all(len(session._pending[sid]) == 0 for sid in session.sources)):
                    time.sleep(0.05)
                    return
                time.sleep(0.02)

        def runner():
            drainer = threading.Thread(target=drain, daemon=True, name='uplink-pump')
            try:
                session.start()
                drainer.start()
                for uplink in uplinks:
                    uplink.start()
                with self._lock:
                    if self.state == 'running':
                        self.state = 'stopping' if self._stop.is_set() else 'running'
                while not self._stop.is_set():
                    if all(uplink.stopped.is_set() for uplink in uplinks):
                        break
                    time.sleep(self._poll_interval)
            finally:
                if self._interrupted.is_set():
                    for uplink in uplinks:
                        uplink.stop_interrupt()
                else:
                    wait_capture_drained()
                    for uplink in uplinks:
                        uplink.stop_clean()
                for uplink in uplinks:
                    uplink.join(timeout=15)
                self._stop.set()
                drainer.join(timeout=2)
                summary = session.stop()
                self._record_result(summary)

        threading.Thread(target=runner, daemon=True, name='capture-runner').start()

    def _record_result(self, summary):
        sources = {}
        for uplink in self._uplinks:
            sources[uplink.source_id] = uplink.stats() | {
                'exit_code': uplink.exit_code, 'exit_reason': uplink.exit_reason}
        if self._interrupted.is_set():
            exit_code = EXIT_INTERRUPT
        else:
            failed = [u.exit_code for u in self._uplinks if u.exit_code != 0]
            exit_code = failed[0] if failed else EXIT_OK
        self.result = {'session_id': self.args['session'], 'exit_code': exit_code,
                       'sources': sources, 'summary': summary}
        self.state = 'stopped'
        self._finished.set()

    def request_stop(self, *, interrupt=False):
        if interrupt:
            self._interrupted.set()
        self._stop.set()
        with self._lock:
            if self.state == 'running':
                self.state = 'stopping'

    def wait(self, timeout=None):
        return self._finished.wait(timeout)

    def stats(self):
        with self._lock:
            uplinks, session = list(self._uplinks), self._session
        rows = {}
        for uplink in uplinks:
            rows[uplink.source_id] = uplink.stats() | {
                'connected': not uplink.stopped.is_set(),
                'exit_code': uplink.exit_code, 'exit_reason': uplink.exit_reason}
        return {'state': self.state, 'session_id': self.args.get('session'),
                'sources': rows}
