"""Module integration: real auth/advisor processes + fake gateway/STT.

Skipped when auth/advisor/llm-gateway are not installed in this interpreter.
"""
import importlib.util
from pathlib import Path
import tempfile
import unittest

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

    def test_step_replay_waits_after_each_line(self):
        transcript = self.transcript()
        run = ReplayRun(self.stack, transcript, mode='step', owner=self.owner,
                        settle_timeout_s=90)
        run.open()
        try:
            first = run.send_next()
            assert first is not None
            self.assertEqual(first.text, '我们过一下预算')
            self.assertTrue(run.collect().complete_jobs)
            second = run.send_next()
            assert second is not None
            self.assertEqual(second.text, '我补充一点')
            self.assertIsNone(run.send_next())
            result = run.collect()
            self.assertGreaterEqual(len(result.complete_jobs), 2)
            self.assertTrue(result.replies)
            run.finish()
        finally:
            run.close()


if __name__ == '__main__':
    unittest.main()
