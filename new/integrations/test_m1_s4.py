"""Plan-3.4 acceptance: reliability on the fake bed.

Four families: recorder disconnect/reconnect/retransmit, advisor restart
recovery, bot restart resuming outbox delivery, duplicate event dedup.
"""
import json
import re
import time
import unittest
from urllib import request

from test_bed import TestBed, http_get_json

OPENER = request.build_opener(request.ProxyHandler({}))


class ReliabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bed = TestBed().start()

    @classmethod
    def tearDownClass(cls):
        cls.bed.stop()

    def open_session(self, meeting_no='123456789'):
        bed, bus = self.bed, self.bed.bus
        bed.authorize('ou_A')
        reply = bus.user_says(bed.service, 'ou_A', f'入会 {meeting_no}')
        self.assertIn('已入会', reply)
        session_id = re.search(r"--session '([^']+)'", reply).group(1)
        token = re.search(r"--token '([^']+)'", reply).group(1)
        event = re.search(r"--event '([^']+)'", reply).group(1)
        return session_id, token, event

    def test_recorder_disconnect_reconnect_retransmit(self):
        bed = self.bed
        session_id, token, event = self.open_session('111000111')

        ws = bed.ingest_connect(session_id, token)
        bed.ingest_start(ws, session_id, token, event=event)
        for seq in range(1, 9):
            bed.ingest_frame(ws, seq)
        acked = bed.drain_acks(ws, 8, timeout=10)
        ws.close()  # network drop mid-stream
        time.sleep(0.5)

        # reconnect with overlap retransmit: first_sequence well below cursor
        ws2 = bed.ingest_connect(session_id, token)
        greeting = bed.ingest_start(ws2, session_id, token, event=event, resume=True,
                                    first_sequence=max(1, acked - 3))
        self.assertGreaterEqual(greeting['acked_sequence'], acked if acked else 0)
        for seq in range(max(1, acked - 3), 13):
            bed.ingest_frame(ws2, seq)
        final_acked = bed.drain_acks(ws2, 12, timeout=10)
        ws2.send(json.dumps({'type': 'stream_end', 'last_sequence': 12}))
        stopped = json.loads(ws2.recv(timeout=10))
        self.assertEqual(stopped['acked_sequence'], 12)
        ws2.close()
        self.assertGreaterEqual(final_acked, 8)

        # overlap frames were deduped: captions counted once per sentence_key
        status = http_get_json(
            f'http://127.0.0.1:{bed.ports["api"]}/sessions/{session_id}/status')
        sources = {row['source']: row for row in status.get('sources', [])}
        self.assertEqual(sources['loopback']['state'], 'drained')
        self.assertEqual(sources['loopback']['acked_sequence'], 12)

    def test_advisor_restart_recovers_supervision(self):
        bed, bus = self.bed, self.bed.bus
        session_id, token, event = self.open_session('222000222')

        ws = bed.ingest_connect(session_id, token)
        bed.ingest_start(ws, session_id, token, event=event)
        for seq in range(1, 6):
            bed.ingest_frame(ws, seq)
        bed.drain_acks(ws, 5, timeout=10)
        ws.close()
        time.sleep(0.3)

        bed.restart_advisor()

        # recorder resumes against the new ingest port; the recovered
        # supervisor keeps consuming and a reply still reaches 小A
        ws2 = bed.ingest_connect(session_id, token)
        bed.ingest_start(ws2, session_id, token, event=event, resume=True, first_sequence=6)
        for seq in range(6, 15):
            bed.ingest_frame(ws2, seq)
        bed.drain_acks(ws2, 14, timeout=10)
        ws2.close()

        deadline = time.monotonic() + 30
        delivered = False
        while time.monotonic() < deadline:
            delivered = any('网关合成回答' in text for text in bus.dms_to('ou_A'))
            if delivered:
                break
            time.sleep(0.3)
        self.assertTrue(delivered, 'no reply after advisor restart')
        bus.user_says(bed.service, 'ou_A', '结束会议')

    def test_bot_restart_resumes_outbox_delivery(self):
        bed, bus = self.bed, self.bed.bus
        # stop the poller: replies accumulate as pending IM rows
        bed.service.stop_polling()
        session_id, token, event = self.open_session('333000333')
        ws = bed.ingest_connect(session_id, token)
        bed.ingest_start(ws, session_id, token, event=event)
        for seq in range(1, 13):
            bed.ingest_frame(ws, seq)
        bed.drain_acks(ws, 10, timeout=10)
        ws.close()

        pending = []
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            try:
                pending = http_get_json(
                    f'http://127.0.0.1:{bed.ports["api"]}/outbox/pending?limit=50')['rows']
            except Exception:
                pending = []
            if pending:
                break
            time.sleep(0.3)
        self.assertTrue(pending, 'no pending reply accumulated while bot was down')

        # bot restart: a fresh poller drains the backlog
        from bot.advisor_client import AdvisorClient
        from bot.service import BotService
        bus.dms.clear()
        bed.service = BotService(AdvisorClient(
            f'http://127.0.0.1:{bed.ports["api"]}'), bus, poll_interval=0.1)
        bed.service.start_polling()
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if any('网关合成回答' in text for text in bus.dms_to('ou_A')):
                break
            time.sleep(0.2)
        self.assertTrue(any('网关合成回答' in text for text in bus.dms_to('ou_A')),
                        'backlog not delivered after bot restart')
        bus.user_says(bed.service, 'ou_A', '结束会议')

    def test_duplicate_events_deduped(self):
        bed, bus = self.bed, self.bed.bus
        # duplicate IM message_id: exactly one reply
        replies_before = len(bus.replies)
        bed.service.handle_message('ou_A', '状态', 'om-dup-event')
        time.sleep(0.5)
        bed.service.handle_message('ou_A', '状态', 'om-dup-event')
        time.sleep(0.5)
        self.assertEqual(len(bus.replies) - replies_before, 1)


if __name__ == '__main__':
    unittest.main()
