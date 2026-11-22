"""T17 acceptance: decision-v2 Markdown replies keep their format through outbox.

The advisor requests `decision-v2`, the gateway fake model returns a reply, and
the bot poller hands an OutboundMessage tagged `markdown` to the IM transport.
"""
import time
import unittest

from test_bed import TestBed


class RichTextReplyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bed = TestBed(extra_advisor_args=('--decision-output', 'decision-v2')).start()

    @classmethod
    def tearDownClass(cls):
        cls.bed.stop()

    def test_reply_reaches_bot_tagged_as_markdown(self):
        bed, bus = self.bed, self.bed.bus
        bed.authorize('ou_A')
        _, reply = bus.join_says(bed.service, 'ou_A', '入会 123456789')
        self.assertIn('已入会', reply)
        rows = bed.advisor_db_read(
            "SELECT session_id, stream_token FROM sessions WHERE owner_open_id='ou_A' "
            "ORDER BY started_at DESC LIMIT 1")
        self.assertTrue(rows, 'joined session not found in advisor db')
        session_id, token = rows[0]

        ws = bed.ingest_connect(session_id, token)
        bed.ingest_start(ws, session_id, token)
        for seq in range(1, 13):
            bed.ingest_frame(ws, seq)
        bed.drain_acks(ws, 10)

        deadline = time.monotonic() + 30
        delivered = []
        while time.monotonic() < deadline:
            with bus._lock:
                delivered = [(target, message) for target, message in bus.dms
                             if target == 'ou_A' and '网关合成回答' in message]
            if delivered:
                break
            time.sleep(0.2)
        self.assertTrue(delivered, f'no reply dm; dms={bus.dms_to("ou_A")[-3:]}')
        message = delivered[0][1]
        self.assertEqual(getattr(message, 'msg_format', None), 'markdown',
                         f'expected markdown outbox row, got {message!r}')
        ws.close()


if __name__ == '__main__':
    unittest.main()
