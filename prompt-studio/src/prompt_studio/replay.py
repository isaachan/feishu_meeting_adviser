"""Per-run replay orchestration: fresh advisor -> material -> captions -> jobs.

A run owns one advisor process and one session. The stack's auth/gateway/fake
STT are shared and long-lived. ``mode='step'`` sends one human line and waits
for the decision to settle; ``mode='auto'`` sends all lines, drains the source
and waits once.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from pathlib import Path
import secrets
import time
from uuid import uuid4

from .clients import AdvisorClient, ClientError, RecorderArgs
from .ingest import IngestError, IngestSession
from .material import MaterialPatchError, patch_material
from .results import (Job, ReplayResult, ResultError, read_jobs, read_material,
                      read_records, read_session)
from .stack import ProcessHandle, Stack, StackError, free_port, wait_healthz
from .transcript import Transcript, TranscriptError


class ReplayError(RuntimeError):
    pass


class ReplayTimeout(ReplayError):
    pass


@dataclass
class ReplayOptions:
    owner: str | None = None
    meeting_no: str | None = None
    run_id: str | None = None
    mode: str = 'auto'
    caption_timeout_s: float = 30.0
    settle_timeout_s: float = 260.0
    poll_s: float = 0.2
    keepalive_s: float = 30.0
    advisor_extra_args: tuple[str, ...] = field(default_factory=tuple)


def new_meeting_no() -> str:
    return str(secrets.randbelow(900_000_000) + 100_000_000)


def new_run_id() -> str:
    return time.strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:6]


class ReplayRun:
    def __init__(self, stack: Stack, transcript: Transcript, *,
                 mode: str = 'auto', owner: str | None = None,
                 meeting_no: str | None = None, run_id: str | None = None,
                 caption_timeout_s: float = 30.0,
                 settle_timeout_s: float = 260.0,
                 poll_s: float = 0.2,
                 keepalive_s: float = 30.0,
                 advisor_extra_args: tuple[str, ...] = ()):
        self.stack = stack
        self.transcript = transcript
        self.options = ReplayOptions(
            owner=owner, meeting_no=meeting_no, run_id=run_id, mode=mode,
            caption_timeout_s=caption_timeout_s,
            settle_timeout_s=settle_timeout_s, poll_s=poll_s,
            keepalive_s=keepalive_s,
            advisor_extra_args=tuple(advisor_extra_args))
        self.owner = owner or stack.config.owner
        self.meeting_no = meeting_no or new_meeting_no()
        self.run_id = run_id or new_run_id()
        self.run_dir = stack.data_dir / 'runs' / self.run_id
        self.advisor_dir = self.run_dir / 'advisor'
        self.advisor_log = self.run_dir / 'advisor.log'
        self.advisor_db = self.advisor_dir / 'adviser.sqlite3'
        self.api_url = ''
        self.session_id: str | None = None
        self.recorder: RecorderArgs | None = None
        self.advisor: AdvisorClient | None = None
        self.ingest: IngestSession | None = None
        self._process: ProcessHandle | None = None
        self._process_name = f'advisor-{self.run_id}'
        self._opened = False
        self._ingest_ended = False
        self._index = 0
        self._expected: Counter[str] = Counter()
        self._last_keepalive = time.monotonic()
        self._snapshots: dict[str, dict] = {}

    @property
    def human_turns(self):
        return self.transcript.human_turns

    def open(self) -> 'ReplayRun':
        if self._opened:
            return self
        if self.stack.stt is None or self.stack.auth is None:
            raise ReplayError('stack is not started')
        try:
            self.transcript.validate()
        except TranscriptError as exc:
            raise ReplayError(str(exc)) from None
        if self.run_dir.exists():
            raise ReplayError(f'run directory already exists: {self.run_dir}')
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.run_dir.chmod(0o700)
        self.advisor_dir.mkdir(mode=0o700)
        try:
            self._spawn_advisor()
            try:
                self.stack.ensure_owner(self.owner)
            except ClientError as exc:
                raise ReplayError(self._client_message('authorize owner', exc)) from None
            self.advisor = AdvisorClient(self.api_url)
            try:
                response = self.advisor.create_session(self.owner, self.meeting_no)
            except ClientError as exc:
                raise ReplayError(self._client_message('create session', exc)) from None
            session_id = response.get('session_id')
            if not isinstance(session_id, str) or not session_id:
                raise ReplayError('create session response has no session_id')
            self.session_id = session_id
            self.recorder = AdvisorClient.recorder_args(response)
            try:
                patch_material(self.advisor_db, session_id,
                               self.transcript.description,
                               self.transcript.documents)
            except MaterialPatchError as exc:
                raise ReplayError(str(exc)) from None
            self.stack.stt.register(session_id, 'mic',
                                    [turn.text for turn in self.human_turns])
            self.ingest = IngestSession(self.recorder)
            try:
                self.ingest.start()
            except IngestError as exc:
                raise ReplayError(f'ingest start failed: {exc}') from None
            self._opened = True
            self._last_keepalive = time.monotonic()
            return self
        except BaseException:
            self.close()
            raise

    def _spawn_advisor(self):
        assert self.stack.stt is not None
        last_error: StackError | None = None
        for attempt in range(5):
            self._spawn_advisor_once()
            assert self._process is not None
            try:
                wait_healthz(self.api_url + '/healthz', self._process.process,
                             self._process.log_path,
                             self.stack.config.health_timeout)
                return
            except StackError as exc:
                last_error = exc
                self._stop_advisor()
                if 'address already in use' not in str(exc):
                    raise ReplayError(str(exc)) from None
                time.sleep(0.1 * (attempt + 1))
        raise ReplayError(str(last_error)) from None

    def _spawn_advisor_once(self):
        assert self.stack.stt is not None
        api_port, ingest_port = free_port(), free_port()
        self.api_url = f'http://127.0.0.1:{api_port}'
        args = [
            'serve', '--data-dir', str(self.advisor_dir),
            '--api-port', str(api_port), '--ingest-port', str(ingest_port),
            '--ingest-url', f'ws://127.0.0.1:{ingest_port}/ingest',
            '--stt-url', self.stack.stt.url,
            '--gateway-url', self.stack.gateway_url,
            '--auth-url', self.stack.auth_url,
            '--model', 'remote',
            '--remote-backend', self.stack.config.backend,
            '--decision-output', 'decision-v2',
            '--calendar', 'stub', '--calendar-stub-description', '',
            '--nsdp', 'stub', '--nsdp-nag-s', '2',
            *self.options.advisor_extra_args,
        ]
        self._process = self.stack.spawn_process(
            self._process_name, 'advisor', args, log_path=self.advisor_log)

    def _stop_advisor(self):
        if self._process is None:
            return
        self._process.stop()
        if self.stack.processes.get(self._process_name) is self._process:
            self.stack.processes.pop(self._process_name, None)
        self._process = None

    @staticmethod
    def _client_message(action: str, exc: ClientError) -> str:
        message = f'{action} failed: {exc}'
        missing = exc.payload.get('missing_credentials')
        if isinstance(missing, list):
            parts = []
            for item in missing:
                if not isinstance(item, dict):
                    continue
                provider = item.get('provider') or '?'
                url = item.get('submit_url') or item.get('authorize_url') or ''
                parts.append(f'{provider}: {url}')
            if parts:
                message += '; missing credentials -> ' + '; '.join(parts)
        return message

    def send_next(self):
        if not self._opened or self.ingest is None:
            raise ReplayError('run is not open')
        turns = self.human_turns
        if self._index >= len(turns):
            return None
        turn = turns[self._index]
        self._index += 1
        self._expected[turn.text] += 1
        self.ingest.send_turn()
        self._wait_caption(turn.text, self._expected[turn.text])
        self.wait_settled()
        return turn

    def finish(self):
        if self.ingest is not None and not self._ingest_ended:
            self.ingest.end()
            self._ingest_ended = True

    def _wait_caption(self, text: str, expected: int):
        self._wait_captions(Counter({text: expected}))

    def _wait_captions(self, expected: Counter[str]):
        deadline = time.monotonic() + self.options.caption_timeout_s
        while time.monotonic() < deadline:
            self._ensure_alive()
            try:
                records = read_records(self.advisor_db, self.session_id or '')
            except ResultError:
                records = []
            counts = Counter(record['text'] for record in records
                             if record.get('type') == 'human')
            if all(counts[text] >= count for text, count in expected.items()):
                return
            self._keepalive()
            time.sleep(self.options.poll_s)
        raise ReplayTimeout(
            f'captions did not land: {dict(expected)}; log={self.advisor_log}\n'
            f'{self._tail()}')

    def wait_settled(self, timeout: float | None = None) -> list[Job]:
        if not self._opened:
            raise ReplayError('run is not open')
        deadline = time.monotonic() + (
            self.options.settle_timeout_s if timeout is None else timeout)
        error_since = None
        session_id = self.session_id or ''
        while True:
            self._ensure_alive()
            try:
                state = read_session(self.advisor_db, session_id)
                jobs = read_jobs(self.advisor_db, session_id)
            except ResultError:
                state, jobs = None, []
            self._remember_snapshots(jobs)
            if state is not None:
                active = [job for job in jobs if not job.settled]
                if (state.generation <= state.decided_generation
                        and not active):
                    return jobs
                if state.error:
                    if error_since is None:
                        error_since = time.monotonic()
                    elif (time.time() >= state.retry_at
                          and time.monotonic() - error_since >= 3.0):
                        return jobs
                else:
                    error_since = None
            if time.monotonic() >= deadline:
                raise ReplayTimeout(
                    f'decision not settled: {state}; log={self.advisor_log}\n'
                    f'{self._tail()}')
            self._keepalive()
            time.sleep(self.options.poll_s)

    def _remember_snapshots(self, jobs: list[Job]):
        for job in jobs:
            snapshot = job.snapshot
            if isinstance(snapshot, dict) and isinstance(snapshot.get('captions'), list):
                self._snapshots.setdefault(job.job_id, snapshot)

    def _keepalive(self):
        if self.ingest is None or self._ingest_ended:
            return
        now = time.monotonic()
        if now - self._last_keepalive < self.options.keepalive_s:
            return
        try:
            self.ingest.keepalive()
        except IngestError:
            return
        self._last_keepalive = now

    def _ensure_alive(self):
        if self._process is not None and self._process.process.poll() is not None:
            raise ReplayError(
                f'advisor exited during replay; log={self.advisor_log}\n'
                f'{self._tail()}')

    def _tail(self, limit: int = 4000) -> str:
        try:
            return self.advisor_log.read_text(encoding='utf-8',
                                              errors='replace')[-limit:]
        except OSError:
            return ''

    def collect(self) -> ReplayResult:
        if self.session_id is None:
            raise ReplayError('run has no session')
        try:
            state = read_session(self.advisor_db, self.session_id)
            jobs = read_jobs(self.advisor_db, self.session_id)
            material = read_material(self.advisor_db, self.session_id)
            records = read_records(self.advisor_db, self.session_id)
        except ResultError as exc:
            raise ReplayError(str(exc)) from None
        jobs = [replace(job, snapshot=self._snapshots.get(job.job_id, job.snapshot))
                for job in jobs]
        return ReplayResult(
            run_id=self.run_id, session_id=self.session_id,
            meeting_no=self.meeting_no, transcript=self.transcript,
            session_state=state, jobs=jobs, records=records,
            material=material, advisor_log=str(self.advisor_log))

    def run_auto(self) -> ReplayResult:
        if self.options.mode != 'auto':
            raise ReplayError('run_auto requires mode=auto')
        self.open()
        try:
            if self.ingest is None:
                raise ReplayError('ingest missing')
            for _ in self.human_turns:
                self.ingest.send_turn()
            self._wait_captions(Counter(turn.text for turn in self.human_turns))
            self.finish()
            self.wait_settled()
            return self.collect()
        finally:
            self.close()

    def close(self):
        if self.ingest is not None and not self._ingest_ended:
            self.ingest.abort()
        if (self.session_id is not None and self.stack.stt is not None):
            self.stack.stt.forget(self.session_id)
        self._stop_advisor()
        self._opened = False

    def __enter__(self) -> 'ReplayRun':
        return self.open()

    def __exit__(self, *exc):
        self.close()
