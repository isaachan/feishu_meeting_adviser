from dataclasses import asdict, replace
import unittest

from feishu_meeting_adviser.llm import LLMSettings
from feishu_meeting_adviser.models import Caption, epoch_ns
from feishu_meeting_adviser.snapshot import SnapshotBuilder
from feishu_meeting_adviser.storage import encode


AT = '2026-10-13T00:00:01Z'
NOW = epoch_ns(AT) / 1_000_000_000


class FakeSnapshotData:
    def __init__(self, captions, *, material=None, sent=None, pending=None):
        self._meeting = {
            'generation': len(captions),
            'decided_generation': 0,
            'participation': 'active',
            'source_state': 'collecting',
            'source_status': None,
        }
        self._rows = [
            {'body': encode(asdict(caption)), 'suppressed': 0, 'generation': index}
            for index, caption in enumerate(captions, 1)
        ][::-1]
        self._material = material
        self._sent = sent or []
        self._pending = pending

    def meeting(self, meeting_id):
        return self._meeting if meeting_id == 'm1' else None

    def context_rows(self, meeting_id, *, limit):
        return self._rows[:limit]

    def pending_reply_row(self, meeting_id):
        return self._pending

    def recent_reply_rows(self, meeting_id, *, limit):
        return self._sent[:limit]

    def context_count(self, meeting_id):
        return len(self._rows)

    def materials(self, meeting_id):
        return self._material


class SnapshotBuilderTests(unittest.TestCase):
    def test_fake_data_builds_evidence_without_sqlite(self):
        participant = Caption(
            'm1', 's:participant', 'fresh question', AT,
            event_time=AT, speaker_id='participant')
        own = replace(participant, sentence_key='s:bot', text='bot text', speaker_id='bot')
        result = SnapshotBuilder(FakeSnapshotData([participant, own])).build(
            'm1', LLMSettings(context_characters=10_000),
            now=NOW, joined_at=NOW - 10, bot_id='bot')
        self.assertEqual(result['fresh_input_keys'], ['s:participant'])
        self.assertEqual(set(result['evidence']), {'s:participant', 's:bot'})
        self.assertEqual([row['sentence_key'] for row in result['captions']],
                         ['s:bot', 's:participant'])

    def test_budget_drops_old_replies_and_truncates_material_before_captions(self):
        caption = Caption(
            'm1', 's:1', 'keep this whole sentence', AT,
            event_time=AT, speaker_id='participant')
        material = {
            'state': 'ready',
            'description': '',
            'documents': [{'text': 'm' * 2_000, 'truncated': False}],
        }
        sent = [{'content': 'r' * 1_000, 'state': 'sent'}]
        settings = LLMSettings(context_characters=2_000)
        result = SnapshotBuilder(FakeSnapshotData(
            [caption], material=material, sent=sent)).build(
                'm1', settings, now=NOW, joined_at=NOW - 10)
        self.assertEqual(result['sent_replies'], [])
        self.assertEqual(result['captions'][0]['text'], 'keep this whole sentence')
        self.assertTrue(result['meeting_context']['documents'][0]['truncated'])
        self.assertLessEqual(len(encode(result)), settings.context_characters)


if __name__ == '__main__':
    unittest.main()
