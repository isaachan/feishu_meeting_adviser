"""T5 GUI e2e：macos_gui 客户端 ↔ 真 advisor/auth 进程（集成床）。

覆盖：device-flow 扫码登录（stub OAuth，code 即 open_id，discovery 采纳）→
/gui/sessions 发现会话（args 指向床 ingest，流令牌与建会一致）→
真 CaptureJob 推流 + 干净 stream_end → 源 drained → 结束会议 →
draining 时 args 置 null → 登出后 401。
"""
import json
import tempfile
import time
import unittest
from pathlib import Path
from urllib import parse, request

from macos_gui.api import ApiError, GuiApiClient
from macos_gui.capture import CaptureJob
from recorder.audio import FakeSource
from test_bed import TestBed

OPENER = request.build_opener(request.ProxyHandler({}))


def http_post_json(url, body, key=None):
    headers = {'Content-Type': 'application/json'}
    if key:
        headers['X-Idempotency-Key'] = key
    req = request.Request(url, data=json.dumps(body).encode(), method='POST',
                          headers=headers)
    with OPENER.open(req, timeout=10) as response:
        return response.status, json.loads(response.read())


def wait_until(predicate, timeout=15.0, interval=0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class GuiE2eTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bed = TestBed().start()
        cls.api = GuiApiClient(f'http://127.0.0.1:{cls.bed.ports["api"]}')

    @classmethod
    def tearDownClass(cls):
        cls.bed.stop()

    def scan_login(self, open_id):
        """QR login with the stub browser (code IS the open_id)."""
        start = self.api.login_start()
        self.assertTrue(start['login_url'].startswith('https://accounts.feishu.cn/'))
        state = parse.parse_qs(parse.urlsplit(start['login_url']).query)['state'][0]
        callback = (f'http://127.0.0.1:{self.bed.ports["auth"]}/oauth/callback?'
                    + parse.urlencode({'code': open_id, 'state': state}))
        with OPENER.open(request.Request(callback, method='GET'), timeout=10) as page:
            self.assertIn('登录成功', page.read().decode('utf-8'))
        result = None

        def completed():
            nonlocal result
            result = self.api.login_poll(start['device_code'])
            return result.get('status') == 'completed'

        self.assertTrue(wait_until(completed, timeout=20))
        return result

    def source_drained(self, session_id):
        url = f'http://127.0.0.1:{self.bed.ports["api"]}/sessions/{session_id}/status'
        with OPENER.open(request.Request(url, method='GET'), timeout=10) as response:
            status = json.loads(response.read())
        return any(s['source'] == 'loopback' and s.get('state') == 'drained'
                   for s in status.get('sources', []))

    def test_gui_full_chain(self):
        bed = self.bed

        # 1) 扫码登录 → device_token + 身份（discovery 采纳扫码者）
        poll = self.scan_login('ou_A')
        device_token = poll['device_token']
        self.assertTrue(device_token.startswith('dt-'))
        self.assertEqual(poll['user']['open_id'], 'ou_A')
        self.assertEqual(poll['user']['email'], 'ou_A@stub.local')

        # 2) 空列表 → 入会（凭据已由扫码落库，无需再授权）
        self.assertEqual(self.api.sessions(device_token)['sessions'], [])
        status, created = http_post_json(
            f'http://127.0.0.1:{bed.ports["api"]}/sessions',
            {'owner_open_id': 'ou_A', 'meeting_ref': {'no': '123456789'}},
            key='gui-e2e-join-1')
        self.assertEqual(status, 200)
        session_id = created['session_id']

        # 3) GUI 发现会话：args 与建会响应一致、指向床 ingest
        snapshot = self.api.sessions(device_token)
        item = next(s for s in snapshot['sessions'] if s['session_id'] == session_id)
        self.assertEqual(item['phase'], 'recording')
        args = item['recorder_command_args']
        self.assertEqual(args['session'], session_id)
        self.assertEqual(args['token'], created['recorder_command_args']['token'])
        self.assertEqual(args['url'],
                         f'ws://127.0.0.1:{bed.ports['ingest']}/ingest')

        # 4) 真 CaptureJob 推流（FakeSource）→ ack → 干净停止
        with tempfile.TemporaryDirectory() as tmp:
            source = FakeSource('loopback')
            job = CaptureJob(args, out_root=Path(tmp), sources=[source])
            job.start()
            self.assertTrue(wait_until(lambda: source._sink is not None),
                            'capture session never started')
            for _ in range(12):
                source.push_frames(1)
                time.sleep(0.02)
            self.assertTrue(wait_until(lambda: job.stats()['sources']['loopback']
                                       ['acked_sequence'] >= 6), 'frames not acked')
            job.request_stop()
            self.assertTrue(job.wait(timeout=15))
            self.assertEqual(job.result['exit_code'], 0)
            self.assertTrue((Path(tmp) / 'loopback.wav').exists())

        # 5) 源 drained（stream_end 已确认）
        self.assertTrue(wait_until(lambda: self.source_drained(session_id)),
                        'source never drained')

        # 6) 结束会议 → draining/ended → GUI 列表 args 置 null
        status, ended = http_post_json(
            f'http://127.0.0.1:{bed.ports["api"]}/sessions/{session_id}/end',
            {'owner_open_id': 'ou_A'})
        self.assertIn(ended['phase'], ('draining', 'ended'))

        def args_null():
            for item in self.api.sessions(device_token)['sessions']:
                if item['session_id'] == session_id:
                    return item['recorder_command_args'] is None
            return False

        self.assertTrue(wait_until(args_null, timeout=20))
        self.api.logout(device_token)
        with self.assertRaises(ApiError) as ctx:
            self.api.sessions(device_token)
        self.assertEqual((ctx.exception.status, ctx.exception.code),
                         (401, 'bad_token'))


if __name__ == '__main__':
    unittest.main()
