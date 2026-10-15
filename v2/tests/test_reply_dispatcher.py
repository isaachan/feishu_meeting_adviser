from types import SimpleNamespace
import unittest

from feishu_meeting_adviser.client import ApiError, ErrorKind
from feishu_meeting_adviser.reply_dispatcher import ReplyDispatcher


class FakeData:
    def __init__(self):
        self.row = {
            'uuid': 'synthetic-uuid',
            'meeting_id': 'm1',
            'content': 'synthetic reply',
        }
        self.calls = []

    def discard_expired_replies(self, meeting_id, settings, **kwargs):
        self.calls.append(('discard', meeting_id, kwargs))
        return 0

    def next_reply(self, meeting_id, *, now):
        self.calls.append(('next', meeting_id, now))
        return self.row

    def mark_sending(self, message_uuid):
        self.calls.append(('sending', message_uuid))

    def mark_sent(self, message_uuid, *, now, interval):
        self.calls.append(('sent', message_uuid, now, interval))

    def mark_send_error(self, message_uuid, reason, *, uncertain):
        self.calls.append(('error', message_uuid, reason, uncertain))


class FakeSource:
    def __init__(self, backlog=0):
        self.count = backlog

    def backlog(self, meeting_id, consumer_id):
        return self.count


class FakeClient:
    def __init__(self, error=None):
        self.error = error
        self.sent = []

    def send_message(self, meeting_id, content, message_uuid):
        self.sent.append((meeting_id, content, message_uuid))
        if self.error:
            raise self.error


class ReplyDispatcherTests(unittest.TestCase):
    def dispatcher(self, *, backlog=0, error=None):
        data = FakeData()
        client = FakeClient(error)
        dispatcher = ReplyDispatcher(
            data,
            FakeSource(backlog),
            client,
            meeting_id='m1',
            consumer_id='adviser',
            settings=SimpleNamespace(freshness_seconds=120, reply_interval_seconds=10),
            session={'joined_at': 1, 'bot_user_id': 'bot'},
            clock=lambda: 2,
        )
        return dispatcher, data, client

    def test_backlog_blocks_context_check_and_send(self):
        dispatcher, data, client = self.dispatcher(backlog=1)
        self.assertFalse(dispatcher.dispatch())
        self.assertEqual(data.calls, [])
        self.assertEqual(client.sent, [])

    def test_confirmed_send_preserves_state_side_effect_order(self):
        dispatcher, data, client = self.dispatcher()
        self.assertTrue(dispatcher.dispatch())
        self.assertEqual([call[0] for call in data.calls], ['discard', 'next', 'sending', 'sent'])
        self.assertEqual(client.sent, [('m1', 'synthetic reply', 'synthetic-uuid')])

    def test_network_failure_becomes_uncertain_without_confirming_sent(self):
        dispatcher, data, client = self.dispatcher(
            error=ApiError(ErrorKind.NETWORK))
        self.assertFalse(dispatcher.dispatch())
        self.assertEqual([call[0] for call in data.calls], ['discard', 'next', 'sending', 'error'])
        self.assertEqual(data.calls[-1], ('error', 'synthetic-uuid', 'network', True))
        self.assertEqual(len(client.sent), 1)


if __name__ == '__main__':
    unittest.main()
