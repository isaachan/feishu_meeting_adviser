import unittest

from harness.decision import validate_decision

NONE_DECISION = {'control': 'none', 'control_sentence_keys': [],
                 'reply': None, 'reply_sentence_keys': []}


class ValidateDecisionTests(unittest.TestCase):
    def test_valid_none(self):
        decision, reason = validate_decision(dict(NONE_DECISION))
        self.assertIsNone(reason)
        self.assertEqual(decision, NONE_DECISION)

    def test_valid_reply(self):
        value = dict(NONE_DECISION, reply='注意时间', reply_sentence_keys=['s0001'])
        self.assertIsNone(validate_decision(value)[1])

    def test_valid_cancel(self):
        value = {'control': 'cancel', 'control_sentence_keys': ['s0001'],
                 'reply': None, 'reply_sentence_keys': []}
        self.assertIsNone(validate_decision(value)[1])

    def test_non_dict(self):
        self.assertIsNotNone(validate_decision(None)[1])
        self.assertIsNotNone(validate_decision('none')[1])

    def test_missing_and_extra_fields(self):
        self.assertIsNotNone(validate_decision({})[1])
        extra = dict(NONE_DECISION, extra=1)
        self.assertIsNotNone(validate_decision(extra)[1])

    def test_bad_control(self):
        self.assertIsNotNone(validate_decision(dict(NONE_DECISION, control='pause'))[1])

    def test_none_with_keys(self):
        value = dict(NONE_DECISION, control_sentence_keys=['s0001'])
        self.assertIsNotNone(validate_decision(value)[1])

    def test_reply_key_mismatch(self):
        value = dict(NONE_DECISION, reply='回复')
        self.assertIsNotNone(validate_decision(value)[1])
        value = dict(NONE_DECISION, reply_sentence_keys=['s0001'])
        self.assertIsNotNone(validate_decision(value)[1])

    def test_cancel_with_reply(self):
        value = {'control': 'cancel', 'control_sentence_keys': ['s0001'],
                 'reply': '不能发了', 'reply_sentence_keys': ['s0002']}
        self.assertIsNotNone(validate_decision(value)[1])

    def test_duplicate_and_bad_keys(self):
        value = dict(NONE_DECISION, reply='a', reply_sentence_keys=['s1', 's1'])
        self.assertIsNotNone(validate_decision(value)[1])
        value = dict(NONE_DECISION, reply='a', reply_sentence_keys=[''])
        self.assertIsNotNone(validate_decision(value)[1])
        value = dict(NONE_DECISION, reply='a', reply_sentence_keys=[1])
        self.assertIsNotNone(validate_decision(value)[1])

    def test_oversize_reply(self):
        value = dict(NONE_DECISION, reply='x' * 4001, reply_sentence_keys=['s1'])
        self.assertIsNotNone(validate_decision(value)[1])

    def test_blank_reply(self):
        value = dict(NONE_DECISION, reply='   ', reply_sentence_keys=['s1'])
        self.assertIsNotNone(validate_decision(value)[1])


if __name__ == '__main__':
    unittest.main()
