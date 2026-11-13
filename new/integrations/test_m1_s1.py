"""Plan-3.2 acceptance: S1 main chain end to end on the fake bed.

小A says 入会 -> authorization link -> fake OAuth -> retry -> calendar check ->
session + stream token -> recorder command rendered (red line asserted) ->
fake recorder pushes audio -> ingest -> fake STT -> captions persisted.
"""
import json
import re
import time
import unittest
from urllib import request

from test_bed import TestBed, http_get_json

OPENER = request.build_opener(request.ProxyHandler({}))


class S1MainChainTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bed = TestBed().start()

    @classmethod
    def tearDownClass(cls):
        cls.bed.stop()

    def test_s1_full_chain(self):
        bed, bus = self.bed, self.bed.bus

        # 1) instruction without credentials -> authorization link
        ack, reply = bus.join_says(bed.service, 'ou_A', '入会 123456789')
        self.assertIn('正在入会', ack)
        self.assertIn('授权', reply)
        link = re.search(r'https?://\S+', reply)
        self.assertTrue(link, reply)

        # 2) fake OAuth in the browser role, then retry the instruction
        bed.authorize('ou_A')
        _, reply = bus.join_says(bed.service, 'ou_A', '入会 123456789')
        self.assertIn('已入会', reply)
        self.assertIn('recorder real', reply)
        # red line: no user credentials in the command text
        self.assertNotIn('stub-ou_A', reply)
        self.assertNotIn('u-at-', reply)

        # 3) extract the command args and run a fake recorder over ingest
        session_id = re.search(r"--session '([^']+)'", reply).group(1)
        token = re.search(r"--token '([^']+)'", reply).group(1)
        event = re.search(r"--event '([^']+)'", reply).group(1)
        self.assertTrue(session_id.startswith('s-'))
        self.assertEqual(event, 'cal_stub/evt_123456789')

        ws = bed.ingest_connect(session_id, token)
        greeting = bed.ingest_start(ws, session_id, token)
        self.assertEqual(greeting['acked_sequence'], 0)
        for seq in range(1, 13):
            bed.ingest_frame(ws, seq)
        acked = bed.drain_acks(ws, 10)
        self.assertGreaterEqual(acked, 10)
        ws.send(json.dumps({'type': 'stream_end', 'last_sequence': 12}))
        stopped = json.loads(ws.recv(timeout=10))
        self.assertEqual(stopped, {'type': 'stopped', 'acked_sequence': 12})
        ws.close()

        # 4) captions flowed through the fake STT into the session store
        deadline = time.monotonic() + 15
        status = {}
        while time.monotonic() < deadline:
            status = http_get_json(
                f'http://127.0.0.1:{bed.ports["api"]}/sessions/{session_id}/status')
            if status.get('pending_model_inputs', 0) or status.get('model_state') != 'idle':
                break
            time.sleep(0.2)
        self.assertEqual(status.get('phase'), 'recording')
        self.assertTrue(status.get('material_present'), status)
        self.assertGreaterEqual(status.get('pending_model_inputs', 0), 1,
                                f'no captions landed: {status}')

        # 5) source drained in status projection
        sources = {row['source']: row for row in status.get('sources', [])}
        self.assertEqual(sources.get('loopback', {}).get('state'), 'drained')

    def test_s1_rejections_live(self):
        bed, bus = self.bed, self.bed.bus
        # meeting link with password parses and reaches the (stub) calendar path
        _, reply = bus.join_says(bed.service, 'ou_A',
                                '入会 https://vc.feishu.cn/j/987654321?pwd=abc')
        self.assertIn('已入会', reply)
        _, reply = bus.join_says(bed.service, 'ou_A', '入会 987654321')
        self.assertIn('已在服务中', reply)  # duplicate join idempotence


if __name__ == '__main__':
    unittest.main()
