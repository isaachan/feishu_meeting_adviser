"""Module integration: real auth/advisor processes + fake gateway/STT.

Skipped when auth/advisor/llm-gateway are not installed in this interpreter.
"""
import importlib.util
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from prompt_studio import cli
from prompt_studio.replay import ReplayRun
from prompt_studio.stack import Stack, StackConfig
from prompt_studio.transcript import parse_text

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
class StackReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='prompt-studio-test-')
        cls.addClassCleanup(cls.temp.cleanup)
        cls.stack = Stack(StackConfig(data_dir=Path(cls.temp.name) / 'data',
                                      backend='fake'))
        cls.addClassCleanup(cls.stack.stop)
        cls.stack.start()
        cls.owner = 'ou_prompt_studio_test'
        cls.stack.ensure_owner(cls.owner, nsdp_token='stub-nsdp-token',
                               jira_token='stub-jira-token')

    def transcript(self):
        transcript = parse_text(SAMPLE)
        transcript.description = '本次会议讨论预算'
        transcript.documents = [{'kind': 'doc', 'title': '预算表',
                                 'text': '总预算 500 万'}]
        return transcript

    def test_auto_replay_produces_decision_and_reply(self):
        transcript = self.transcript()
        result = ReplayRun(self.stack, transcript, mode='auto',
                           owner=self.owner, settle_timeout_s=90).run_auto()
        self.assertTrue(result.session_id.startswith('s-'))
        assert result.session_state is not None
        self.assertIsNone(result.session_state.error)
        self.assertTrue(result.complete_jobs, result.jobs)
        decision = result.complete_jobs[-1].decision
        self.assertIsInstance(decision, dict)
        self.assertEqual(decision['control'], 'none')
        self.assertEqual(decision['reply'], '网关合成回答')
        human = [record['text'] for record in result.records
                 if record['type'] == 'human']
        self.assertEqual(human, [turn.text for turn in transcript.human_turns])
        self.assertIn('网关合成回答', result.replies)
        assert result.material is not None
        self.assertEqual(result.material['state'], 'ready')
        self.assertEqual(result.material['description'], transcript.description)
        self.assertEqual(result.material['documents'][0]['text'], '总预算 500 万')
        self.assertEqual(result.material['context_version'],
                         result.session_state.material_version)

    def test_paced_replay_spaces_lines_without_waiting(self):
        transcript = self.transcript()
        started = time.monotonic()
        result = ReplayRun(self.stack, transcript, mode='paced',
                           owner=self.owner, settle_timeout_s=90).run_paced(0.05)
        self.assertGreaterEqual(time.monotonic() - started, 0.05)
        self.assertTrue(result.complete_jobs, result.jobs)
        human = [record['text'] for record in result.records
                 if record['type'] == 'human']
        self.assertEqual(human, [turn.text for turn in transcript.human_turns])
        self.assertIn('网关合成回答', result.replies)


if __name__ == '__main__':
    unittest.main()


@unittest.skipUnless(modules_installed(), 'auth/advisor/llm-gateway not installed')
class CliReplayTests(unittest.TestCase):
    def test_cli_init_and_auto_replay(self):
        with tempfile.TemporaryDirectory(prefix='prompt-studio-cli-') as temp:
            root = Path(temp)
            data_dir = root / 'data'
            transcript = root / 'meeting.txt'
            transcript.write_text(SAMPLE, encoding='utf-8')
            document = root / '预算表.txt'
            document.write_text('总预算 500 万', encoding='utf-8')
            nsdp = root / 'nsdp.txt'
            nsdp.write_text('stub-nsdp-token', encoding='utf-8')
            jira = root / 'jira.txt'
            jira.write_text('stub-jira-token', encoding='utf-8')
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = cli.main(['init', '--data-dir', str(data_dir)])
            self.assertEqual(code, 0)
            real_stack = Stack
            seen = {}

            def fake_stack(config):
                seen['jira_url'] = config.jira_url
                config.backend = 'fake'
                return real_stack(config)

            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(cli, 'Stack', side_effect=fake_stack), \
                    redirect_stdout(out), redirect_stderr(err):
                code = cli.main([
                    'replay', '--transcript', str(transcript),
                    '--description', 'CLI 测试', '--document', str(document),
                    '--data-dir', str(data_dir), '--owner', 'ou_cli_test',
                    '--nsdp-token-file', str(nsdp),
                    '--jira-token-file', str(jira),
                    '--jira-url', 'https://jira.example.com', '--json'])
            self.assertEqual(code, 0, err.getvalue())
            payload = json.loads(out.getvalue())
            complete = [job for job in payload['jobs']
                        if job['state'] == 'complete']
            self.assertTrue(complete, payload)
            self.assertEqual(seen['jira_url'], 'https://jira.example.com')
            self.assertEqual(complete[-1]['decision']['reply'], '网关合成回答')
            self.assertEqual(payload['material']['description'], 'CLI 测试')
            self.assertEqual(payload['material']['documents'][0]['title'], '预算表')
            self.assertIn('网关合成回答',
                          [record['text'] for record in payload['records']
                           if record['type'] == 'llm'])
