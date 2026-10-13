from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from feishu_meeting_adviser.__main__ import main
from feishu_meeting_adviser.client import ApiError, ErrorKind
from feishu_meeting_adviser.config import load_config
from feishu_meeting_adviser.lifecycle import SessionManager
from feishu_meeting_adviser.models import Caption
from feishu_meeting_adviser.storage import DurableFakeConsumer, StorageError, Store
from test_lifecycle import BotAPI, MID


class DeadlineRuleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_path = Path(self.tmp.name) / 'config.yaml'
        self.config_path.write_text('app:\n  id: synthetic\n  secret: synthetic-secret\nmeeting:\n  meeting_no: "012345678"\n')
        self.config_path.chmod(0o600)
        self.config = load_config(self.config_path)
        self.store = Store(self.config.data_directory / 'source.sqlite3')
        self.addCleanup(self.store.close)
        self.api = BotAPI()
        self.manager = SessionManager(self.store, self.config, self.api)
        self.manager.join()
        self.store.ensure_meeting(MID, {})
        self.caption = Caption(MID, 's:1', '项目截止日期是明天。', '2026-10-13T00:00:00Z',
                               speaker_name='合成说话人甲', source_revision=1)
        self.sent = []
        self.failure = None
        self.api.send_message = self.send_message

    def send_message(self, mid, text, message_uuid):
        self.sent.append((mid, text, message_uuid))
        if self.failure:
            raise self.failure

    def consume(self, expected=0):
        output, errors = io.StringIO(), io.StringIO()
        with patch('feishu_meeting_adviser.__main__.FeishuClient', return_value=self.api), \
             redirect_stdout(output), redirect_stderr(errors):
            result = main(['--config', str(self.config_path), 'consume'])
        self.assertEqual(result, expected)
        return (json.loads(output.getvalue()) if output.getvalue() else None), errors.getvalue()

    def commit(self, *captions):
        self.store.commit_page(MID, list(captions), {})

    def test_matching_caption_sends_actual_speaker_to_saved_meeting_once(self):
        self.commit(self.caption)
        summary, errors = self.consume()
        self.assertEqual(self.sent[0][:2], (MID, '合成说话人甲，项目截止日期是今天。'))
        self.assertEqual(summary['replied'], 1)
        self.assertTrue(summary['acknowledged'])
        self.assertEqual(errors, '')
        self.assertEqual(self.store.backlog(MID, 'fake-qa'), 0)
        self.consume()
        self.assertEqual(len(self.sent), 1)

    def test_nonmatching_missing_speaker_and_conflict_do_not_reply(self):
        self.commit(replace(self.caption, text='项目截止日期是今天'),
                    replace(self.caption, sentence_key='s:2', speaker_name=None),
                    replace(self.caption, text='项目截止日期是明天', source_revision=None))
        self.assertEqual(self.consume()[0]['replied'], 0)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.store.backlog(MID, 'fake-qa'), 0)

    def test_substring_whitespace_and_two_speakers_get_independent_replies(self):
        self.commit(replace(self.caption, text='请注意，项目 截止日期是明天！'),
                    replace(self.caption, sentence_key='s:2', speaker_name='合成说话人乙'))
        self.assertEqual(self.consume()[0]['replied'], 2)
        self.assertEqual([row[1] for row in self.sent], ['合成说话人甲，项目截止日期是今天。', '合成说话人乙，项目截止日期是今天。'])
        self.assertNotEqual(self.sent[0][2], self.sent[1][2])

    def test_revision_of_replied_sentence_does_not_send_again(self):
        self.commit(self.caption)
        self.consume()
        self.commit(replace(self.caption, text='再强调一下，项目截止日期是明天。', source_revision=2))
        self.assertEqual(self.consume()[0]['replied'], 0)
        self.assertEqual(len(self.sent), 1)

    def test_send_failure_preserves_failed_input_and_reuses_uuid_after_restart(self):
        self.commit(replace(self.caption, sentence_key='s:0', text='普通发言'), self.caption)
        self.failure = ApiError(ErrorKind.NETWORK)
        _, errors = self.consume(expected=1)
        self.assertIn('Check meeting UI', errors)
        self.assertNotIn('合成说话人', errors)
        self.assertEqual(self.store.backlog(MID, 'fake-qa'), 1)
        self.failure = None
        self.assertEqual(self.consume()[0]['replied'], 1)
        self.assertEqual(self.sent[0], self.sent[1])
        self.assertEqual(self.store.backlog(MID, 'fake-qa'), 0)

    def test_sent_reply_survives_crash_before_source_ack(self):
        self.commit(self.caption)
        with patch.object(Store, 'acknowledge', side_effect=StorageError('synthetic crash before ack')):
            self.consume(expected=1)
        self.assertEqual(self.store.backlog(MID, 'fake-qa'), 1)
        self.assertEqual(self.consume()[0]['replied'], 0)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.store.backlog(MID, 'fake-qa'), 0)

    def test_left_session_does_not_send_and_reply_storage_is_deleted_with_meeting(self):
        self.commit(self.caption)
        self.manager.leave()
        self.consume(expected=1)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.store.backlog(MID, 'fake-qa'), 1)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(['--config', str(self.config_path), 'delete-meeting-data', '--confirm']), 0)
        for path in (self.config.data_directory / 'fake_consumers').glob('*.sqlite3'):
            consumer = DurableFakeConsumer(path)
            try:
                self.assertEqual(consumer.db.execute('SELECT count(*) FROM rule_replies').fetchone()[0], 0)
            finally:
                consumer.close()
