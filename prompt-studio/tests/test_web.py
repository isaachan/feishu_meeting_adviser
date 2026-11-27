"""HTTP-level tests for the local prompt-studio web UI."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
from http.server import ThreadingHTTPServer
import threading
import time
import unittest
from urllib import error, request

from prompt_studio.stack import PROMPT_FILES, Stack, StackConfig
from prompt_studio.transcript import parse_text
from prompt_studio.web_app import StudioApp
from prompt_studio.web_http import make_handler

SAMPLE = """会参谋 · 文字记录
会话 s-old · 会议 111111111 · 预算会
导出时间 2026/11/26 16:00:00 · 共 3 条

[2026/11/26 16:02:03] 本机: 我们过一下预算
[时间未知] LLM: 历史回复
[2026/11/26 16:02:10] 未识别: 我补充一点
"""


def modules_installed() -> bool:
    try:
        return all(importlib.util.find_spec(name) is not None
                   for name in ('auth', 'advisor', 'llm_gateway'))
    except (ImportError, ValueError):
        return False


@unittest.skipUnless(modules_installed(), 'auth/advisor/llm-gateway not installed')
class WebIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from prompt_studio.cli import _gateway_prompts

        cls.temp = tempfile.TemporaryDirectory(prefix='prompt-studio-web-')
        cls.addClassCleanup(cls.temp.cleanup)
        root = Path(cls.temp.name)
        cls.prompt_dir = root / 'data' / 'prompts'
        cls.prompt_dir.mkdir(parents=True)
        for name in PROMPT_FILES:
            shutil.copyfile(_gateway_prompts() / name, cls.prompt_dir / name)
        cls.stack = Stack(StackConfig(
            data_dir=root / 'data', prompt_dir=cls.prompt_dir, backend='fake'))
        cls.addClassCleanup(cls.stack.stop)
        cls.stack.start()
        cls.owner = 'ou_prompt_studio_web'
        cls.stack.ensure_owner(cls.owner, nsdp_token='stub-nsdp-token',
                               jira_token='stub-jira-token')
        cls.app = StudioApp(cls.stack, cls.prompt_dir, cls.owner)
        cls.addClassCleanup(cls.app.close)
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0),
                                         make_handler(cls.app))
        cls.server.daemon_threads = True
        cls.addClassCleanup(cls.server.server_close)
        cls.thread = threading.Thread(target=cls.server.serve_forever,
                                      kwargs={'poll_interval': 0.05},
                                      daemon=True)
        cls.thread.start()
        cls.addClassCleanup(cls.server.shutdown)
        port = cls.server.server_address[1]
        cls.base = f'http://127.0.0.1:{port}'

    def call(self, method: str, path: str, payload=None,
             content_type: str = 'application/json'):
        data = None
        headers = {}
        if payload is not None:
            data = json.dumps(payload).encode('utf-8')
            headers['Content-Type'] = content_type
        req = request.Request(self.base + path, data=data, method=method,
                              headers=headers)
        opener = request.build_opener(request.ProxyHandler({}))
        try:
            with opener.open(req, timeout=10) as response:
                body = response.read()
                return response.status, json.loads(body) if body else None
        except error.HTTPError as exc:
            try:
                body = exc.read()
                return exc.code, json.loads(body) if body else None
            finally:
                exc.close()

    def wait_run(self, predicate, timeout: float = 120.0) -> dict:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status, run = self.call('GET', '/api/run')
            self.assertEqual(status, 200, run)
            if predicate(run):
                return run
            time.sleep(0.1)
        self.fail(f'run did not reach expected state: {run}')

    def test_static_and_config(self):
        status, body = self.call('GET', '/api/health')
        self.assertEqual((status, body['status']), (200, 'ok'))
        status, config = self.call('GET', '/api/config')
        self.assertEqual(status, 200)
        self.assertEqual(config['owner'], self.owner)
        self.assertEqual(config['backend'], 'fake')
        opener = request.build_opener(request.ProxyHandler({}))
        with opener.open(self.base + '/', timeout=10) as response:
            html = response.read().decode('utf-8')
        self.assertEqual(response.status, 200)
        self.assertIn('Prompt Studio', html)

    def test_prompts_read_write_and_traversal(self):
        status, body = self.call('GET', '/api/prompts')
        self.assertEqual(status, 200)
        self.assertEqual(set(body['files']), set(PROMPT_FILES))
        original = body['files']['agent_prompt.txt']
        status, body = self.call(
            'PUT', '/api/prompts/agent_prompt.txt', {'content': '测试提示词\n'})
        self.assertEqual(status, 200, body)
        status, body = self.call('GET', '/api/prompts')
        self.assertEqual(body['files']['agent_prompt.txt'], '测试提示词\n')
        self.call('PUT', '/api/prompts/agent_prompt.txt', {'content': original})
        status, body = self.call(
            'PUT', '/api/prompts/..%2Fsecret.txt', {'content': 'x'})
        self.assertEqual(status, 404, body)
        status, body = self.call(
            'PUT', '/api/prompts/agent_prompt.txt', {'content': 'x' * 200_001})
        self.assertEqual(status, 413, body)

    def test_parse_rejects_bad_text(self):
        status, body = self.call('POST', '/api/parse', {'text': 'no records'})
        self.assertEqual(status, 400, body)
        status, scene = self.call('POST', '/api/parse', {'text': SAMPLE})
        self.assertEqual(status, 200)
        self.assertEqual(len(scene['turns']), 3)
        self.assertEqual(
            [turn['text'] for turn in scene['turns'] if turn['kind'] == 'human'],
            ['我们过一下预算', '我补充一点'])

    def test_auto_replay_over_http(self):
        transcript = parse_text(SAMPLE)
        status, run = self.call('POST', '/api/run/start', {
            'text': SAMPLE, 'description': 'Web 测试',
            'title': '预算会', 'mode': 'auto'})
        self.assertEqual(status, 201, run)
        self.assertEqual(run['state'], 'opening')
        run = self.wait_run(lambda item: item['state'] in ('done', 'error'))
        self.assertEqual(run['state'], 'done', run.get('error'))
        result = run['result']
        self.assertEqual(result['title'], '预算会')
        self.assertEqual(result['description'], 'Web 测试')
        complete = [job for job in result['jobs'] if job['state'] == 'complete']
        self.assertTrue(complete)
        self.assertEqual(complete[-1]['decision']['reply'], '网关合成回答')
        self.assertEqual(result['material']['description'], 'Web 测试')
        self.assertEqual(
            [turn.text for turn in transcript.human_turns],
            [record['text'] for record in result['records']
             if record['type'] == 'human'])

    def test_step_replay_over_http(self):
        status, run = self.call('POST', '/api/run/start',
                                {'text': SAMPLE, 'mode': 'step'})
        self.assertEqual(status, 201, run)
        run = self.wait_run(lambda item: item['state'] == 'step')
        for expected in (1, 2):
            status, body = self.call('POST', '/api/run/next')
            self.assertEqual(status, 200, body)
            run = self.wait_run(
                lambda item: len(item['steps']) >= expected, timeout=90)
        status, body = self.call('POST', '/api/run/finish')
        self.assertEqual(status, 200, body)
        run = self.wait_run(lambda item: item['state'] in ('done', 'error'))
        self.assertEqual(run['state'], 'done', run.get('error'))
        self.assertEqual(len(run['steps']), 2)
        self.assertTrue(run['result']['jobs'])
        self.assertIn('网关合成回答',
                      [record['text'] for record in run['result']['records']
                       if record['type'] == 'llm'])

    def test_switch_model_restarts_gateway(self):
        status, config = self.call('POST', '/api/config',
                                   {'model': 'zai-coding-plan/glm-4.6'})
        self.assertEqual(status, 200, config)
        self.assertEqual(config['model'], 'zai-coding-plan/glm-4.6')
        status, body = self.call('POST', '/api/config', {'model': 'bad model'})
        self.assertEqual(status, 400, body)
        status, body = self.call('POST', '/api/config', {'model': 'fake/x'},
                                 content_type='text/plain')
        self.assertEqual(status, 415, body)


if __name__ == '__main__':
    unittest.main()
