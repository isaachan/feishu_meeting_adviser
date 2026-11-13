"""Plan-3.1 integration test bed: process-level advisor/auth/llm-gateway,
fake STT WS server, fake Feishu IM bus driving the in-process BotService.

Everything binds to ephemeral ports; stop() is the one-shot teardown.
"""
import asyncio
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from urllib import request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..'))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def http_get_json(url, timeout=2):
    opener = request.build_opener(request.ProxyHandler({}))
    with opener.open(request.Request(url, method='GET'), timeout=timeout) as response:
        return json.loads(response.read())


def wait_healthz(url, timeout=40):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if http_get_json(url).get('status') == 'ok':
                return True
        except Exception:
            time.sleep(0.1)
    return False


class FakeSttServer:
    """Contract-v2-shaped STT stub: every ~3 PCM chunks emits one final."""

    def __init__(self, texts=None):
        self.texts = texts or ['今天我们讨论预算方案', '第二句话讲进度', '最后一句总结']
        self.finals = []
        self._utterance = 0
        self.loop = asyncio.new_event_loop()
        self.started = threading.Event()

    def start(self):
        threading.Thread(target=self._serve, daemon=True).start()
        self.started.wait(timeout=5)
        return self

    def _serve(self):
        asyncio.set_event_loop(self.loop)
        index = [0]

        async def handler(ws):
            start = json.loads(await ws.recv())
            await ws.send(json.dumps({'type': 'started', 'protocol': start.get('protocol', 2),
                                      'model': 'fake-stt', 'user_id': 'stub',
                                      'calendar_event_id': 'stub'}))
            offset = 0
            fed = 0
            async for raw in ws:
                if not isinstance(raw, bytes):
                    message = json.loads(raw)
                    if message.get('type') == 'stop':
                        await ws.send(json.dumps({'type': 'stopped', 'finals': len(self.finals)}))
                        await ws.close()
                        return
                    continue  # gap: nothing to flush in the stub
                samples = len(raw) // 2
                fed += 1
                if fed % 3 == 0:
                    text = self.texts[index[0]] if index[0] < len(self.texts) else (
                        f'第{index[0] + 1}句会议内容')
                    index[0] += 1
                    end = offset + samples
                    self.finals.append({'text': text, 'start_offset': offset, 'end_offset': end})
                    await ws.send(json.dumps({'type': 'final', 'text': text,
                                              'start_offset': offset, 'end_offset': end}))
                offset += samples

        async def main():
            import websockets
            async with websockets.serve(handler, '127.0.0.1', 0,
                                        max_size=2 ** 21) as server:
                self.port = server.sockets[0].getsockname()[1]
                self.started.set()
                try:
                    while True:
                        await asyncio.sleep(0.05)
                finally:
                    server.close()
                    await server.wait_closed()
        try:
            self.loop.run_until_complete(main())
        except BaseException as exc:  # noqa: BLE001 - surface bed startup faults
            import traceback
            traceback.print_exc(file=sys.stderr)
            self.start_error = exc
            self.started.set()

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)

    @property
    def url(self):
        return f'ws://127.0.0.1:{self.port}'


class FakeImBus:
    """Fake Feishu IM: drives BotService and captures every outbound message."""

    def __init__(self):
        self.replies = []   # (message_id, text)
        self.dms = []       # (open_id, text)
        self._counter = 0
        self._lock = threading.Lock()

    # ImTransport surface
    def send_reply(self, message_id, text):
        with self._lock:
            self.replies.append((message_id, text))
        return f'om-reply-{len(self.replies)}'

    def send_dm(self, open_id, text):
        with self._lock:
            self.dms.append((open_id, text))
        return f'om-dm-{len(self.dms)}'

    def user_says(self, service, open_id, text):
        self._counter += 1
        with self._lock:
            before_replies = len(self.replies)
        service.handle_message(open_id, text, f'om-in-{self._counter}')
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with self._lock:
                if len(self.replies) > before_replies:
                    return self.replies[-1][1]
            time.sleep(0.02)
        return ''

    def join_says(self, service, open_id, text, *, timeout=20):
        """Send a slow 入会 command (T6): ack reply is immediate, the real
        outcome (success command / error link) arrives as a DM; returns both."""
        with self._lock:
            before = len(self.dms)
        ack = self.user_says(service, open_id, text)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if len(self.dms) > before:
                    return ack, self.dms[-1][1]
            time.sleep(0.02)
        raise AssertionError('join outcome dm never arrived')

    def dms_to(self, open_id):
        with self._lock:
            return [text for target, text in self.dms if target == open_id]


class TestBed:
    """One-shot bring-up: auth(stub) + gateway(fake) + advisor(stub calendar) +
    fake STT + in-process BotService with the fake IM bus.

    nsdp='stub' additionally enables the advisor NSDP gate with the stub
    fetch client and fast refresh/retry intervals (real auth-side submission
    flow over HTTP); default 'off' keeps prior beds unchanged.
    """

    def __init__(self, *, nsdp='off'):
        self.tmp = tempfile.mkdtemp(prefix='m1-bed-')
        self.processes = []
        self.ports = {}
        self.stt = None
        self.bus = None
        self.service = None
        self.nsdp_mode = nsdp

    def _advisor_args(self):
        args = ['--model', 'remote', '--remote-backend', 'fake',
                '--calendar-stub', '--calendar-stub-description', '集成床日历材料']
        if self.nsdp_mode == 'stub':
            args += ['--nsdp', 'stub',
                     '--nsdp-refresh-s', '2', '--nsdp-retry-s', '1', '--nsdp-nag-s', '2']
        else:
            args += ['--nsdp', 'off']
        return args

    def start(self):
        last_error = None
        for _attempt in range(2):
            try:
                self._start_once()
                return self
            except Exception as exc:  # noqa: BLE001 - cold-start flake retry
                last_error = exc
                self.stop()
                self.processes = []
                self.stt = None
                self.ports = {}
                self.tmp = tempfile.mkdtemp(prefix='m1-bed-')
        raise AssertionError(f'bed failed to start twice: {last_error}')

    def _start_once(self):
        env = dict(os.environ)
        auth_port, gateway_port, api_port, ingest_port = (free_port() for _ in range(4))
        self.ports = {'auth': auth_port, 'gateway': gateway_port,
                      'api': api_port, 'ingest': ingest_port}

        self._spawn([sys.executable, '-m', 'auth', 'serve', '--stub',
                     '--data-dir', os.path.join(self.tmp, 'auth'),
                     '--port', str(auth_port),
                     '--base-url', f'http://127.0.0.1:{auth_port}'],
                    env, 'auth')
        self._spawn([sys.executable, '-m', 'llm_gateway', 'serve',
                     '--port', str(gateway_port),
                     '--database', os.path.join(self.tmp, 'gateway.sqlite3')],
                    env, 'gateway')
        self.stt = FakeSttServer().start()
        self._spawn([sys.executable, '-m', 'advisor', 'serve',
                     '--data-dir', os.path.join(self.tmp, 'advisor'),
                     '--api-port', str(api_port), '--ingest-port', str(ingest_port),
                     '--ingest-url', f'ws://127.0.0.1:{ingest_port}/ingest',
                     '--stt-url', self.stt.url,
                     '--gateway-url', f'http://127.0.0.1:{gateway_port}',
                     '--auth-url', f'http://127.0.0.1:{auth_port}'] + self._advisor_args(),
                    env, 'advisor')

        assert wait_healthz(f'http://127.0.0.1:{auth_port}/healthz'), 'auth healthz timeout'
        assert wait_healthz(f'http://127.0.0.1:{gateway_port}/healthz'), 'gateway healthz timeout'
        assert wait_healthz(f'http://127.0.0.1:{api_port}/healthz'), 'advisor healthz timeout'

        from bot.advisor_client import AdvisorClient
        from bot.service import BotService
        self.bus = FakeImBus()
        self.service = BotService(AdvisorClient(f'http://127.0.0.1:{api_port}'),
                                  self.bus, poll_interval=0.1)
        self.service.start_polling()
        return self

    def _spawn(self, command, env, name):
        log = open(os.path.join(self.tmp, f'{name}.log'), 'w')
        proc = subprocess.Popen(command, env=env, cwd=ROOT, stdout=log, stderr=log)
        self.processes.append((name, proc, log))

    def stop(self):
        if self.service is not None:
            self.service.stop_polling()
        if self.stt is not None:
            self.stt.stop()
        for _name, proc, log in self.processes:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        deadline = time.monotonic() + 5
        for _name, proc, _log in self.processes:
            remaining = max(0.1, deadline - time.monotonic())
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                proc.kill()
        for _name, _proc, log in self.processes:
            log.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def log_text(self, name):
        for proc_name, _proc, log in self.processes:
            if proc_name == name:
                with open(log.name) as fh:
                    return fh.read()
        return ''

    def restart_advisor(self):
        """Kill the advisor process and bring it back on fresh ports with the
        same data directory (supervisor restart-recovery path). Rebuilds the
        BotService against the new API port, preserving the fake IM bus."""
        kept = [(name, proc, log) for name, proc, log in self.processes
                if name != 'advisor']
        for name, proc, log in self.processes:
            if name == 'advisor' and proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                log.close()
        self.processes = kept
        api_port, ingest_port = free_port(), free_port()

        def spawn():
            self._spawn([sys.executable, '-m', 'advisor', 'serve',
                         '--data-dir', os.path.join(self.tmp, 'advisor'),
                         '--api-port', str(api_port), '--ingest-port', str(ingest_port),
                         '--ingest-url', f'ws://127.0.0.1:{ingest_port}/ingest',
                         '--stt-url', self.stt.url,
                         '--gateway-url',
                         f'http://127.0.0.1:{self.ports["gateway"]}',
                         '--auth-url', f'http://127.0.0.1:{self.ports["auth"]}']
                        + self._advisor_args(), dict(os.environ), 'advisor')
        self.ports['api'], self.ports['ingest'] = api_port, ingest_port
        spawn()
        assert wait_healthz(f'http://127.0.0.1:{api_port}/healthz'), 'advisor restart healthz'

        from bot.advisor_client import AdvisorClient
        from bot.service import BotService
        self.service.stop_polling()
        self.service = BotService(AdvisorClient(f'http://127.0.0.1:{api_port}'),
                                  self.bus, poll_interval=0.1)
        self.service.start_polling()

    # conveniences for tests --------------------------------------------------

    def authorize(self, open_id):
        """Fake OAuth: fetch an auth URL and click it with code=open_id."""
        start = http_get_json(f'http://127.0.0.1:{self.ports["auth"]}/healthz')
        assert start['status'] == 'ok'
        from urllib.parse import parse_qs, urlsplit
        opener = request.build_opener(request.ProxyHandler({}))
        req = request.Request(
            f'http://127.0.0.1:{self.ports["auth"]}/authorize/start',
            data=json.dumps({'open_id': open_id, 'intent': 'bed'}).encode(),
            method='POST', headers={'Content-Type': 'application/json'})
        with opener.open(req, timeout=5) as response:
            payload = json.loads(response.read())
        state = parse_qs(urlsplit(payload['authorize_url']).query)['state'][0]
        callback = (f'http://127.0.0.1:{self.ports["auth"]}/oauth/callback?'
                    f'code={open_id}&state={state}')
        with opener.open(request.Request(callback), timeout=5) as response:
            page = response.read().decode()
        assert '授权成功' in page, page
        return payload['authorize_url']

    def nsdp_submit(self, open_id, token):
        """Real NSDP submission flow over HTTP: start -> paste form -> saved."""
        from urllib.parse import parse_qs, urlencode, urlsplit
        opener = request.build_opener(request.ProxyHandler({}))
        req = request.Request(
            f'http://127.0.0.1:{self.ports["auth"]}/token-submissions/start',
            data=json.dumps({'open_id': open_id, 'provider': 'nsdp'}).encode(),
            method='POST', headers={'Content-Type': 'application/json'})
        with opener.open(req, timeout=5) as response:
            payload = json.loads(response.read())
        state = parse_qs(urlsplit(payload['submit_url']).query)['state'][0]
        form = request.Request(
            f'http://127.0.0.1:{self.ports["auth"]}/nsdp/submit?state={state}',
            data=urlencode({'token': token}).encode(), method='POST',
            headers={'Content-Type': 'application/x-www-form-urlencoded'})
        with opener.open(form, timeout=5) as response:
            page = response.read().decode()
        assert '已保存' in page, page
        return payload['submit_url']

    def nsdp_invalidate(self, open_id):
        req = request.Request(
            f'http://127.0.0.1:{self.ports["auth"]}/credentials/{open_id}/invalidate',
            data=json.dumps({'provider': 'nsdp'}).encode(), method='POST',
            headers={'Content-Type': 'application/json'})
        opener = request.build_opener(request.ProxyHandler({}))
        with opener.open(req, timeout=5) as response:
            return json.loads(response.read())

    def advisor_db_read(self, query, args=()):
        """Read-only peek into the advisor sqlite (material assertions)."""
        import sqlite3
        path = os.path.join(self.tmp, 'advisor', 'adviser.sqlite3')
        connection = sqlite3.connect(f'file:{path}?mode=ro', uri=True)
        try:
            return connection.execute(query, args).fetchall()
        finally:
            connection.close()

    def ingest_connect(self, session_id, token, source='loopback'):
        """Open a raw ingest WS connection (contract client, test driven)."""
        import websockets.sync.client as sync_client
        return sync_client.connect(
            f'ws://127.0.0.1:{self.ports["ingest"]}/ingest',
            open_timeout=10, close_timeout=5, max_size=2 ** 21, proxy=None)

    @staticmethod
    def ingest_start(ws, session_id, token, *, source='loopback', resume=False,
                     first_sequence=1, event='cal_stub/evt_123456789'):
        import struct
        ws.send(json.dumps({'type': 'start', 'protocol': 1, 'session_id': session_id,
                            'stream_token': token, 'source': source, 'event': event,
                            'resume': resume, 'first_sequence': first_sequence,
                            'wall_clock_at': '2026-11-10T14:00:00+08:00'}))
        greeting = json.loads(ws.recv(timeout=10))
        assert greeting.get('type') == 'started', greeting
        return greeting

    @staticmethod
    def ingest_frame(ws, sequence, samples=1600):
        import struct
        ws.send(struct.pack('<I', sequence) + b'\x01\x00' * samples)

    @staticmethod
    def drain_acks(ws, want_sequence, timeout=15):
        deadline = time.monotonic() + timeout
        acked = 0
        while time.monotonic() < deadline:
            try:
                message = json.loads(ws.recv(timeout=0.5))
            except TimeoutError:
                continue
            if message.get('type') == 'ack':
                acked = max(acked, message['sequence'])
                if acked >= want_sequence:
                    return acked
        return acked
