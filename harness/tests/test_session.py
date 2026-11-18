import unittest

from harness.session import CONTEXT_CHARACTERS, LOCAL_SPEAKER, MeetingSession, caption_fingerprint


class FakeClock:
    def __init__(self, start=1000000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class MeetingSessionTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.session = MeetingSession(clock=self.clock)

    def test_add_caption_increments_generation_and_keys(self):
        self.assertEqual(self.session.add_caption('第一句', LOCAL_SPEAKER), 's0001')
        self.assertEqual(self.session.add_caption('第二句'), 's0002')
        self.assertEqual(self.session.generation, 2)
        self.assertEqual(self.session.pending_count, 2)

    def test_caption_shape(self):
        self.session.add_caption('内容', LOCAL_SPEAKER, duration_ms=1500)
        caption = self.session.captions[0]
        self.assertEqual(caption['speaker_name'], LOCAL_SPEAKER)
        self.assertEqual(caption['conflicted'], False)
        self.assertEqual(caption['end_time'] - caption['start_time'], 1500)
        self.assertEqual(caption['event_time'], caption['end_time'])
        self.assertIn('T', caption['received_at'])

    def test_fresh_keys_cover_undecided_within_window(self):
        self.session.add_caption('第一句')
        self.session.add_caption('第二句')
        snapshot = self.session.snapshot()
        self.assertEqual(snapshot['fresh_input_keys'], ['s0001', 's0002'])
        self.assertEqual(snapshot['generation'], 2)
        self.assertEqual(snapshot['pending_reply'], None)
        self.assertEqual(snapshot['context_omitted'], False)
        self.assertEqual(set(snapshot['evidence']), {'s0001', 's0002'})

    def test_stale_caption_kept_as_context_not_fresh(self):
        self.session.add_caption('旧句')
        self.clock.advance(300)
        self.session.add_caption('新句')
        snapshot = self.session.snapshot()
        self.assertEqual(snapshot['fresh_input_keys'], ['s0002'])
        self.assertEqual(len(snapshot['captions']), 2)
        self.assertEqual(snapshot['reply_allowed_keys'], ['s0001', 's0002'])

    def test_reply_evidence_window_wider_than_fresh(self):
        self.session.add_caption('旧句')
        self.clock.advance(300)
        snapshot = self.session.snapshot()
        self.assertEqual(snapshot['fresh_input_keys'], [])
        self.assertEqual(snapshot['reply_allowed_keys'], ['s0001'])

    def test_caption_before_join_ignored(self):
        self.session.add_caption('太早', now=self.session.started_at - 30)
        snapshot = self.session.snapshot()
        self.assertEqual(snapshot['fresh_input_keys'], [])
        self.assertEqual(snapshot['reply_allowed_keys'], [])

    def test_apply_decision_catches_up_and_records_reply(self):
        self.session.add_caption('一句')
        decision = {'control': 'none', 'control_sentence_keys': [],
                    'reply': '提醒', 'reply_sentence_keys': ['s0001']}
        self.session.apply_decision(decision)
        self.assertEqual(self.session.decided_generation, 1)
        self.assertEqual(self.session.pending_count, 0)
        self.assertEqual(self.session.snapshot()['sent_replies'],
                         [{'text': '提醒', 'state': 'sent'}])
        self.session.add_caption('又一句')
        self.session.apply_decision({'control': 'none', 'control_sentence_keys': [],
                                     'reply': None, 'reply_sentence_keys': []})
        self.assertEqual(self.session.decided_generation, 2)
        self.assertEqual(len(self.session.snapshot()['sent_replies']), 1)

    def test_context_budget_drops_oldest(self):
        session = MeetingSession(context_characters=400, clock=self.clock)
        for i in range(10):
            session.add_caption(f'句子编号{i}内容稍微长一点')
        snapshot = session.snapshot()
        self.assertTrue(snapshot['context_omitted'])
        keys = [item['sentence_key'] for item in snapshot['captions']]
        self.assertNotIn('s0001', keys)
        self.assertIn('s0010', keys)

    def test_default_budget_holds_normal_session(self):
        for i in range(50):
            self.session.add_caption('这是一句普通的会议发言' * 5)
        snapshot = self.session.snapshot()
        self.assertFalse(snapshot['context_omitted'])
        self.assertEqual(len(snapshot['captions']), 50)
        self.assertLess(len(str(snapshot)), CONTEXT_CHARACTERS * 2)

    def test_fingerprint_stable_and_sensitive(self):
        self.session.add_caption('内容', LOCAL_SPEAKER, duration_ms=1000)
        first = caption_fingerprint(self.session.captions[0])
        self.assertEqual(first, caption_fingerprint(self.session.captions[0]))
        changed = dict(self.session.captions[0], text='别的')
        self.assertNotEqual(first, caption_fingerprint(changed))


if __name__ == '__main__':
    unittest.main()
