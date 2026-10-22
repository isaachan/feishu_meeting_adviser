import unittest

from feishu_spectator.models import (ChatMessage, MeetingRef, ParticipantChange,
                                     TranscriptItem, digest, epoch_ms)


class EpochMsTests(unittest.TestCase):
    def test_numeric_milliseconds(self):
        self.assertEqual(epoch_ms(1716699012000), 1716699012000)
        self.assertEqual(epoch_ms('1716699012000'), 1716699012000)

    def test_rfc3339(self):
        self.assertEqual(epoch_ms('2024-05-26T08:50:12Z'), 1716713412000)
        self.assertEqual(epoch_ms('2024-05-26T16:50:12+08:00'), 1716713412000)
        self.assertEqual(epoch_ms('2024-05-26T08:50:12.5Z'), 1716713412500)

    def test_unknown(self):
        self.assertIsNone(epoch_ms(None))
        self.assertIsNone(epoch_ms(True))
        self.assertIsNone(epoch_ms('not-a-time'))
        self.assertIsNone(epoch_ms(1.5))


class ItemTests(unittest.TestCase):
    def test_transcript_kind_and_end_ms(self):
        item = TranscriptItem(meeting_id='737', item_key='s:1', text='hello',
                              received_at='now', sentence_id='1',
                              start_time='1000', end_time='2000')
        self.assertEqual(item.kind.value, 'transcript')
        self.assertEqual(item.end_ms, 2000)
        self.assertFalse(item.identity_degraded)
        payload = item.to_dict()
        self.assertEqual(payload['text'], 'hello')
        self.assertEqual(payload['kind'], 'transcript')

    def test_degraded_flag_passthrough(self):
        item = TranscriptItem(meeting_id='737', item_key='h:x', text='hello',
                              received_at='now', identity_degraded=True)
        self.assertTrue(item.identity_degraded)
        self.assertTrue(item.to_dict()['identity_degraded'])

    def test_chat_and_participant(self):
        chat = ChatMessage(meeting_id='737', item_key='m:1', content='hi', received_at='now')
        self.assertEqual(chat.kind.value, 'chat')
        joined = ParticipantChange(meeting_id='737', item_key='p:1', direction='joined',
                                   received_at='now')
        self.assertEqual(joined.kind.value, 'participant_joined')
        left = ParticipantChange(meeting_id='737', item_key='p:2', direction='left',
                                 received_at='now', leave_reason=2)
        self.assertEqual(left.kind.value, 'participant_left')
        self.assertEqual(left.to_dict()['leave_reason'], 2)

    def test_digest_stable(self):
        self.assertEqual(digest(['a', 1]), digest(['a', 1]))
        self.assertNotEqual(digest(['a', 1]), digest(['a', 2]))

    def test_meeting_ref(self):
        ref = MeetingRef(meeting_id='123456789012', meeting_no='123456789', topic='周会')
        self.assertEqual(ref.to_dict()['topic'], '周会')


if __name__ == '__main__':
    unittest.main()
