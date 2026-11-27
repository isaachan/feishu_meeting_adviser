"""Long-lived services for a replay session: auth(stub), gateway and fake STT.

The advisor is per-run (see ``replay``) so every run starts from a fresh DB and
fresh material; auth and gateway stay up across runs to keep tokens and the
release catalog warm.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
from urllib import error, request

from .clients import AuthClient
from .fake_stt import FakeSttServer

ROOT = Path(__file__).resolve().parents[3]

PROMPT_FILES = ('agent_prompt.txt', 'snapshot_rules.txt', 'reply_markdown_rules.txt')


class StackError(RuntimeError):
    pass


def free_port() -> int:
    # Reserve on all interfaces: the advisor ingest binds 0.0.0.0.
    with socket.socket() as sock:
        sock.bind(('0.0.0.0', 0))
        return sock.getsockname()[1]


def http_json(url: str, timeout: float = 2.0) -> dict:
    opener = request.build_opener(request.ProxyHandler({}))
    with opener.open(request.Request(url, method='GET'), timeout=timeout) as response:
        return json.loads(response.read())


def wait_healthz(url: str, process, log_path: Path,
                 timeout: float = 40.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise StackError(f'{process.args} exited; log={log_path}\n'
                             f'{_tail(log_path)}')
        try:
            if http_json(url).get('status') == 'ok':
                return True
        except (error.URLError, OSError, ValueError):
            pass
        time.sleep(0.1)
    raise StackError(f'healthz timeout: {url}; log={log_path}\n{_tail(log_path)}')


def _tail(path: Path, limit: int = 4000) -> str:
    try:
        return path.read_text(encoding='utf-8', errors='replace')[-limit:]
    except OSError:
        return ''


class ProcessHandle:
    def __init__(self, name: str, process: subprocess.Popen, log, log_path: Path):
        self.name = name
        self.process = process
        self.log = log
        self.log_path = log_path

    def stop(self):
        if self.process.poll() is None:
            try:
                if os.name == 'posix':
                    os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
                else:
                    self.process.terminate()
            except ProcessLookupError:
                pass
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    if os.name == 'posix':
                        os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
                    else:
                        self.process.kill()
                except ProcessLookupError:
                    pass
                self.process.wait(timeout=5)
        if not self.log.closed:
            self.log.close()


@dataclass
class StackConfig:
    root: Path = ROOT
    data_dir: Path | None = None
    prompt_dir: Path | None = None
    backend: str = 'opencode'
    model: str = 'nio/DeepSeek-V4.1-Flash'
    health_timeout: float = 40.0
    opencode_auth: Path | None = None
    opencode_provider_config: Path | None = None
    jira_url: str | None = None
    gateway_extra_args: tuple[str, ...] = ()
    owner: str = 'ou_prompt_studio'

    def directory(self, data_dir: Path | None = None) -> Path:
        return Path(data_dir or self.data_dir or (Path(self.root) / 'data'
                                                  / 'prompt-studio'))


class Stack:
    def __init__(self, config: StackConfig | None = None, *,
                 data_dir: str | Path | None = None):
        self.config = config or StackConfig()
        self.data_dir = self.config.directory(Path(data_dir) if data_dir else None)
        self.ports: dict[str, int] = {}
        self.processes: dict[str, ProcessHandle] = {}
        self.stt: FakeSttServer | None = None
        self.auth: AuthClient | None = None

    def start(self, *, auth_only: bool = False) -> 'Stack':
        if self.processes:
            return self
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(self.data_dir, 0o700)
            (self.data_dir / 'logs').mkdir(mode=0o700, exist_ok=True)
            os.chmod(self.data_dir / 'logs', 0o700)
        except OSError as exc:
            raise StackError(f'cannot prepare private data dir: {exc}') from None
        (self.data_dir / 'auth').mkdir(mode=0o700, exist_ok=True)
        if self.config.prompt_dir is not None and not auth_only:
            missing = [name for name in PROMPT_FILES
                       if not (self.config.prompt_dir / name).is_file()]
            if missing:
                raise StackError(f'prompt dir missing files: {missing}')
        try:
            self.ports = {'auth': free_port()}
            self._spawn_auth()
            if not auth_only:
                self.ports['gateway'] = free_port()
                self._spawn_gateway()
                self.stt = FakeSttServer().start()
            self.auth = AuthClient(self.auth_url)
            self._wait_all(auth_only=auth_only)
        except BaseException:
            self.stop()
            raise
        return self

    def _wait_all(self, *, auth_only: bool = False):
        wait_healthz(self.auth_url + '/healthz', self.processes['auth'].process,
                     self.processes['auth'].log_path, self.config.health_timeout)
        if auth_only:
            return
        wait_healthz(self.gateway_url + '/healthz', self.processes['gateway'].process,
                     self.processes['gateway'].log_path, self.config.health_timeout)

    @property
    def auth_url(self) -> str:
        return f'http://127.0.0.1:{self.ports["auth"]}'

    @property
    def gateway_url(self) -> str:
        return f'http://127.0.0.1:{self.ports["gateway"]}'

    def _env(self) -> dict:
        env = dict(os.environ)
        env.setdefault('PYTHONUNBUFFERED', '1')
        return env

    def spawn_process(self, name: str, module: str, args: list[str], *,
                      log_path: Path | None = None) -> ProcessHandle:
        target = Path(log_path) if log_path is not None else (
            self.data_dir / 'logs' / f'{name}.log')
        target.parent.mkdir(parents=True, exist_ok=True)
        log = open(target, 'a', encoding='utf-8')
        try:
            os.chmod(target, 0o600)
        except OSError:
            pass
        try:
            process = subprocess.Popen(
                [sys.executable, '-m', module, *args], cwd=self.config.root,
                env=self._env(), stdout=log, stderr=log, start_new_session=True)
        except BaseException:
            log.close()
            raise
        handle = ProcessHandle(name, process, log, target)
        self.processes[name] = handle
        return handle

    def _spawn(self, name: str, module: str, args: list[str]) -> ProcessHandle:
        return self.spawn_process(name, module, args)

    def _spawn_auth(self):
        self._spawn('auth', 'auth', [
            'serve', '--stub', '--data-dir', str(self.data_dir / 'auth'),
            '--port', str(self.ports['auth']), '--base-url', self.auth_url])

    def _gateway_args(self) -> list[str]:
        args = ['serve', '--host', '127.0.0.1', '--port', str(self.ports['gateway']),
                '--database', str(self.data_dir / 'gateway.sqlite3'),
                '--auth-url', self.auth_url,
                '--opencode-model', self.config.model]
        if self.config.prompt_dir is not None:
            args += ['--prompt-dir', str(self.config.prompt_dir)]
        if self.config.opencode_auth is not None:
            args += ['--opencode-auth', str(self.config.opencode_auth)]
        if self.config.opencode_provider_config is not None:
            args += ['--opencode-provider-config',
                     str(self.config.opencode_provider_config)]
        if self.config.jira_url:
            args += ['--jira-url', self.config.jira_url]
        args += list(self.config.gateway_extra_args)
        return args

    def _spawn_gateway(self):
        self._spawn('gateway', 'llm_gateway', self._gateway_args())

    def restart_gateway(self, model: str | None = None):
        if model:
            self.config.model = model
        handle = self.processes.pop('gateway', None)
        if handle is not None:
            handle.stop()
        self._spawn_gateway()
        wait_healthz(self.gateway_url + '/healthz',
                     self.processes['gateway'].process,
                     self.processes['gateway'].log_path,
                     self.config.health_timeout)
        return self.gateway_url

    def ensure_owner(self, owner: str | None = None, *,
                     nsdp_token: str | None = None,
                     jira_token: str | None = None) -> str:
        if self.auth is None:
            raise StackError('stack is not started')
        open_id = owner or self.config.owner
        self.auth.authorize(open_id)
        self.submit_tokens(open_id, nsdp_token=nsdp_token, jira_token=jira_token)
        return open_id

    def submit_tokens(self, owner: str, *, nsdp_token: str | None = None,
                      jira_token: str | None = None):
        if self.auth is None:
            raise StackError('stack is not started')
        if nsdp_token:
            self.auth.submit_provider_token(owner, 'nsdp', nsdp_token)
        if jira_token:
            self.auth.submit_provider_token(owner, 'jira', jira_token)

    def stop(self):
        for name in [name for name in self.processes
                     if name.startswith('advisor-')]:
            handle = self.processes.pop(name, None)
            if handle is not None:
                handle.stop()
        if self.stt is not None:
            self.stt.stop()
            self.stt = None
        for name in ('gateway', 'auth'):
            handle = self.processes.pop(name, None)
            if handle is not None:
                handle.stop()

    def __enter__(self) -> 'Stack':
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    def __del__(self):
        try:
            self.stop()
        except Exception:
            pass
