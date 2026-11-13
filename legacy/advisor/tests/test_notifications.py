from pathlib import Path
import tempfile
import unittest

from feishu_meeting_adviser.adviser_store import AdviserStore
from feishu_meeting_adviser.client import ApiError, ErrorKind
from feishu_meeting_adviser.config import NotificationSettings
from feishu_meeting_adviser.llm import Decision, LLMSettings
from feishu_meeting_adviser.reply_dispatcher import ReplyDispatcher
from test_adviser_store import NOW, delivery


class Source:
    def backlog(self, _meeting_id, _consumer_id):
        return 0


class Client:
    def __init__(self, fail_recipient=None, error_kind=ErrorKind.NETWORK):
        self.sent = []
        self.fail_recipient = fail_recipient
        self.error_kind = error_kind

    def send_message(self, meeting_id, content, message_uuid):
        self.sent.append(('meeting', meeting_id, content, message_uuid))

    def send_direct_message(self, open_id, content, message_uuid):
        self.sent.append(('im', open_id, content, message_uuid))
        if open_id == self.fail_recipient:
            raise ApiError(self.error_kind)


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'private' / 'adviser.sqlite3'
        AdviserStore.initialize(self.path)
        self.store = AdviserStore(self.path)
        self.addCleanup(lambda: self.store.close())
        self.settings = LLMSettings(reply_interval_seconds=10)

    def make_reply(self, notifications):
        self.store.ingest('m1', [delivery()])
        job = self.store.prepare_job('m1', self.settings, 'role-v1', now=NOW, joined_at=NOW-10)
        self.store.finish_job(job, Decision('none', (), '合成完整回答', ('s:1',)),
                              now=NOW, settings=self.settings, joined_at=NOW-10,
                              notifications=notifications)

    def dispatcher(self, client):
        return ReplyDispatcher(
            self.store, Source(), client, meeting_id='m1', consumer_id='adviser',
            settings=self.settings, session={'joined_at': NOW-10}, clock=lambda: NOW)

    def test_public_and_private_targets_send_once_independently(self):
        self.make_reply(NotificationSettings(True, ('ou_one', 'ou_two')))
        client = Client()
        dispatcher = self.dispatcher(client)
        self.assertTrue(dispatcher.dispatch())
        self.assertTrue(dispatcher.dispatch())
        self.assertTrue(dispatcher.dispatch())
        self.assertFalse(dispatcher.dispatch())
        self.assertEqual([(channel, target) for channel, target, _, _ in client.sent],
                         [('meeting', 'm1'), ('im', 'ou_one'), ('im', 'ou_two')])
        self.assertEqual(len({message_uuid for _, _, _, message_uuid in client.sent}), 3)
        self.assertEqual(self.store.reply_statistics('m1')['pending_replies'], 0)
        self.store.close()
        self.store = AdviserStore(self.path)
        self.store.recover_sends()
        self.assertFalse(self.dispatcher(client).dispatch())
        self.assertEqual(len(client.sent), 3)
        self.assertEqual(len(self.store.recent_reply_rows('m1', limit=20)), 1)

    def test_uncertain_target_does_not_repeat_confirmed_target(self):
        self.make_reply(NotificationSettings(False, ('ou_one', 'ou_two')))
        client = Client(fail_recipient='ou_one')
        dispatcher = self.dispatcher(client)
        self.assertFalse(dispatcher.dispatch())
        self.assertTrue(dispatcher.dispatch())
        self.assertEqual(self.store.reply_statistics('m1')['uncertain_replies'], 1)
        self.assertEqual(self.store.reply_statistics('m1')['pending_replies'], 0)
        self.store.close()
        self.store = AdviserStore(self.path)
        self.store.recover_sends()
        self.assertFalse(self.dispatcher(client).dispatch())
        self.assertEqual(len(client.sent), 2)

    def test_removed_recipient_cancels_only_unsent_target(self):
        self.make_reply(NotificationSettings(False, ('ou_one', 'ou_two')))
        self.store.reconcile_destinations('m1', NotificationSettings(False, ('ou_two',)))
        client = Client()
        dispatcher = self.dispatcher(client)
        self.assertTrue(dispatcher.dispatch())
        self.assertFalse(dispatcher.dispatch())
        self.assertEqual([target for _, target, _, _ in client.sent], ['ou_two'])

    def test_permission_rejection_is_visible_without_blocking_other_target(self):
        self.make_reply(NotificationSettings(False, ('ou_one', 'ou_two')))
        client = Client(fail_recipient='ou_one', error_kind=ErrorKind.DENIED)
        dispatcher = self.dispatcher(client)
        self.assertFalse(dispatcher.dispatch())
        self.assertTrue(dispatcher.dispatch())
        self.assertEqual(self.store.reply_statistics('m1')['rejected_replies'], 1)
        shown = self.store.list_replies('m1')
        self.assertTrue(any(row['state'] == 'rejected' and row['channel'] == 'im'
                            for row in shown))
        self.assertFalse(any('ou_one' in str(row) for row in shown))


if __name__ == '__main__':
    unittest.main()
