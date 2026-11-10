"""Plan-3.3 acceptance: S2 decision & notification end to end.

Captions -> supervisor job -> gateway fake model -> decision -> outbox
(local + im) -> bot poller -> 小A receives the reply over the fake IM bus.
Then 结束会议 drains to ended; post-ended input is refused.
"""
import json
import re
import time
import unittest
from urllib import request

from test_bed import TestBed, http_get_json

OPENER = request.build_opener(request.ProxyHandler({}))


class S2DecisionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bed = TestBed().start()

    @classmethod
    def tearDownClass(cls):
        cls.bed.stop()

    def start_session_and_stream(self, frames=12):
        bed, bus = self.bed, self.bed.bus
        bed.authorize('ou_A')
        reply = bus.user_says(bed.service, 'ou_A', '入会 123456789')
        self.assertIn('已入会', reply)
        session_id = re.search(r"--session '([^']+)'", reply).group(1)
        token = re.search(r"--token '([^']+)'", reply).group(1)
        ws = bed.ingest_connect(session_id, token)
        bed.ingest_start(ws, session_id, token)
        for seq in range(1, frames + 1):
            bed.ingest_frame(ws, seq)
        bed.drain_acks(ws, frames - 2)
        return session_id, token, ws

    def wait_phase(self, session_id, phase, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            status = http_get_json(
                f'http://127.0.0.1:{self.bed.ports["api"]}/sessions/{session_id}/status')
            if status.get('phase') == phase:
                return status
            time.sleep(0.2)
        return status

    def test_reply_reaches_owner_and_end_drains(self):
        bed, bus = self.bed, self.bed.bus
        session_id, token, ws = self.start_session_and_stream()

        # the gateway fake model replies citing the first fresh input; the bot
        # poller delivers the pending IM row to 小A over the fake bus
        deadline = time.monotonic() + 30
        replies = []
        while time.monotonic() < deadline:
            replies = [text for text in bus.dms_to('ou_A') if '网关合成回答' in text]
            if replies:
                break
            time.sleep(0.2)
        self.assertTrue(replies, f'no reply dm; dms={bus.dms_to("ou_A")[-3:]}')

        # decision idempotency at the outbox level: exactly one delivered reply
        time.sleep(2)
        self.assertEqual(len([t for t in bus.dms_to('ou_A') if '网关合成回答' in t]), 1,
                         f'duplicated reply: {bus.dms_to("ou_A")}')

        # 结束会议 -> draining -> (pending cleared by bot) -> ended
        reply = bus.user_says(bed.service, 'ou_A', '结束会议')
        self.assertIn('排空', reply)
        status = self.wait_phase(session_id, 'ended', timeout=30)
        self.assertEqual(status['phase'], 'ended')

        # ended session refuses new ingest connections
        ws.close()
        time.sleep(0.5)
        ws2 = bed.ingest_connect(session_id, token)
        ws2.send(json.dumps({'type': 'start', 'protocol': 1, 'session_id': session_id,
                             'stream_token': token, 'source': 'loopback',
                             'event': 'cal_stub/evt_123456789', 'resume': True,
                             'first_sequence': 13,
                             'wall_clock_at': '2026-11-10T14:20:00+08:00'}))
        error = json.loads(ws2.recv(timeout=10))
        self.assertEqual((error['type'], error['code']), ('error', 'session_ended'))

    def test_status_command_lists_the_session(self):
        bed, bus = self.bed, self.bed.bus
        bed.authorize('ou_A')
        reply = bus.user_says(bed.service, 'ou_A', '入会 555000111')
        self.assertIn('已入会', reply)
        reply = bus.user_says(bed.service, 'ou_A', '状态')
        self.assertIn('《会议 555000111》', reply)
        self.assertIn('订阅 无', reply)
        bus.user_says(bed.service, 'ou_A', '结束会议')


if __name__ == '__main__':
    unittest.main()
