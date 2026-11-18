import unittest

from harness.client import GatewayError
from harness.repl import HarnessRepl, parse_line
from harness.session import LOCAL_SPEAKER, MeetingSession

NONE_DECISION = {'control': 'none', 'control_sentence_keys': [],
                 'reply': None, 'reply_sentence_keys': []}
REPLY_DECISION = {'control': 'none', 'control_sentence_keys': [],
                  'reply': '提醒一下', 'reply_sentence_keys': ['s0001']}


class StubClient:
    def __init__(self, task=None, error=None):
        self.task = task or {'state': 'succeeded',
                             'result': {'decision': dict(NONE_DECISION), 'timings': {}}}
        self.error = error
        self.url = 'http://stub'
        self.backend = 'stub'
        self.deadline_seconds = 90
        self.snapshots = []
        self.attempts = []
        self._counter = 0

    def content_hash(self, snapshot):
        self._counter += 1
        return f'hash-{self._counter}' if self.error != 'stable' else 'hash-stable'

    def decide(self, snapshot, *, attempt=0):
        self.snapshots.append(snapshot)
        self.attempts.append(attempt)
        if self.error:
            raise GatewayError(self.error)
        return self.task


def make_repl(client, lines):
    session = MeetingSession()
    outputs = []
    feed = iter(lines)
    repl = HarnessRepl(client, session, input_func=lambda _prompt: next(feed),
                       output=outputs.append)
    return repl, session, outputs


class ParseLineTests(unittest.TestCase):
    def test_local_prefixes(self):
        for prefix in ('me:', 'ME:', '我:', '本机:'):
            text, speaker = parse_line(f'{prefix} 这句话')
            self.assertEqual((text, speaker), ('这句话', LOCAL_SPEAKER))

    def test_remote_prefixes(self):
        for prefix in ('其他:', '别的:', '远程:', 'other:', 'Remote:'):
            text, speaker = parse_line(f'{prefix} 那句话')
            self.assertEqual((text, speaker), ('那句话', None))

    def test_no_prefix_is_anonymous_remote(self):
        self.assertEqual(parse_line('没有前缀的发言'), ('没有前缀的发言', None))

    def test_unrelated_colon_passes_through(self):
        line = '注意: 这里有冒号'
        self.assertEqual(parse_line(line), (line, None))


class HarnessReplTests(unittest.TestCase):
    def test_feed_accumulates_without_deciding(self):
        client = StubClient()
        repl, session, outputs = make_repl(client, ['me: 我说一句', '其他: 你说一句', '/quit'])
        repl.run()
        self.assertEqual(len(session.captions), 2)
        self.assertEqual(session.captions[0]['speaker_name'], LOCAL_SPEAKER)
        self.assertIsNone(session.captions[1]['speaker_name'])
        self.assertEqual(client.snapshots, [])
        self.assertIn('未判=1', outputs[1])
        self.assertIn('未判=2', outputs[2])

    def test_decide_submits_and_applies(self):
        client = StubClient(task={'state': 'succeeded',
                                  'result': {'decision': dict(REPLY_DECISION),
                                             'timings': {'queued_ms': 2}}})
        repl, session, outputs = make_repl(client, ['有新句吗', '/decide', '/quit'])
        repl.run()
        self.assertEqual(len(client.snapshots), 1)
        self.assertEqual(client.snapshots[0]['fresh_input_keys'], ['s0001'])
        self.assertEqual(client.attempts, [0])
        self.assertEqual(session.pending_count, 0)
        self.assertEqual(session.snapshot()['sent_replies'][0]['text'], '提醒一下')
        self.assertTrue(any('合法' in line for line in outputs))
        self.assertTrue(any('提醒一下' in line for line in outputs))

    def test_forced_redecide_warns_and_uses_attempt_suffix(self):
        client = StubClient()
        client.content_hash = lambda snapshot: 'same-hash'
        repl, session, outputs = make_repl(client, ['/decide', '/decide', '/decide', '/quit'])
        repl.run()
        self.assertEqual(client.attempts, [0, 1, 2])
        self.assertTrue(any('不会发起任务' in line for line in outputs))
        self.assertTrue(any('-r1' in line for line in outputs))

    def test_decide_without_pending_warns(self):
        client = StubClient()
        client.content_hash = lambda snapshot: 'h1'
        repl, _, outputs = make_repl(client, ['/decide', '/quit'])
        repl.run()
        self.assertTrue(any('不会发起任务' in line for line in outputs))

    def test_decide_error_keeps_state(self):
        client = StubClient(error='backend_unavailable')
        repl, session, outputs = make_repl(client, ['一句话', '/decide', '/quit'])
        repl.run()
        self.assertEqual(session.pending_count, 1)
        self.assertTrue(any('backend_unavailable' in line for line in outputs))

    def test_invalid_decision_not_applied(self):
        client = StubClient(task={'state': 'succeeded',
                                  'result': {'decision': {'control': 'none'},
                                             'timings': {}}})
        repl, session, outputs = make_repl(client, ['一句话', '/decide', '/quit'])
        repl.run()
        self.assertEqual(session.pending_count, 1)
        self.assertTrue(any('非法' in line for line in outputs))

    def test_show_prints_snapshot(self):
        client = StubClient()
        repl, _, outputs = make_repl(client, ['一句话', '/show', '/quit'])
        repl.run()
        shown = '\n'.join(outputs)
        self.assertIn('"fresh_input_keys"', shown)
        self.assertIn('generation=1', shown)

    def test_reset_clears(self):
        client = StubClient()
        repl, _, outputs = make_repl(client, ['一句话', '/reset', '/quit'])
        repl.run()
        self.assertEqual(repl.session.pending_count, 0)
        self.assertEqual(repl.session.captions, [])
        self.assertTrue(any('清空' in line for line in outputs))

    def test_unknown_command_hinted(self):
        client = StubClient()
        repl, _, outputs = make_repl(client, ['/wat', '/quit'])
        repl.run()
        self.assertTrue(any('未知命令' in line for line in outputs))

    def test_empty_lines_skipped(self):
        client = StubClient()
        repl, session, _ = make_repl(client, ['', '   ', '/quit'])
        repl.run()
        self.assertEqual(session.captions, [])


if __name__ == '__main__':
    unittest.main()
