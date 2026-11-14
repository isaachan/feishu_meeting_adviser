"""Plan-3.5: fake-chain latency breakdown (说完 → 字幕 → 决策 → IM 送达).

Coarse polling (100ms) — the fake-chain baseline only; real budgets get
measured again at 6.4 per plan.
"""
import re
import time
import unittest
from urllib import request

from test_bed import TestBed, http_get_json

OPENER = request.build_opener(request.ProxyHandler({}))


class LatencyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bed = TestBed().start()

    @classmethod
    def tearDownClass(cls):
        cls.bed.stop()

    def test_latency_breakdown(self):
        bed, bus = self.bed, self.bed.bus
        bed.authorize('ou_A')
        _, reply = bus.join_says(bed.service, 'ou_A', '入会 777000777')
        session_id = re.search(r"--session '([^']+)'", reply).group(1)
        token = re.search(r"--token '([^']+)'", reply).group(1)
        event = re.search(r"--event '([^']+)'", reply).group(1)

        ws = bed.ingest_connect(session_id, token)
        bed.ingest_start(ws, session_id, token, event=event)
        for seq in range(1, 7):  # warm-up frames (first final flushes pipeline)
            bed.ingest_frame(ws, seq)
        bed.drain_acks(ws, 6, timeout=8)

        # ACK confirms audio ingestion, not completion of STT or the first
        # model decision. Wait for the warm-up reply before establishing the
        # baseline; pending_model_inputs is a transient queue depth.
        warmup_deadline = time.monotonic() + 20
        while time.monotonic() < warmup_deadline:
            if any('网关合成回答' in text for text in bus.dms_to('ou_A')):
                break
            time.sleep(0.1)
        else:
            self.fail('warm-up reply never delivered')
        caption_count = bed.advisor_db_read(
            'SELECT count(*) FROM context WHERE session_id=?', (session_id,))[0][0]

        dm_count_before = len([t for t in bus.dms_to('ou_A') if '网关合成回答' in t])
        t0 = time.monotonic()  # "说完": the utterance's final frames go out now
        for seq in range(7, 13):
            bed.ingest_frame(ws, seq)

        api = f'http://127.0.0.1:{bed.ports["api"]}'
        t_caption = t_decision = t_im = None
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and t_im is None:
            time.sleep(0.1)
            if t_caption is None:
                count = bed.advisor_db_read(
                    'SELECT count(*) FROM context WHERE session_id=?', (session_id,))[0][0]
                if count > caption_count:
                    t_caption = time.monotonic()
            if t_decision is None:
                pending = http_get_json(f'{api}/outbox/pending?limit=5')['rows']
                if pending:
                    t_decision = time.monotonic()
            if len([t for t in bus.dms_to('ou_A') if '网关合成回答' in t]) > dm_count_before:
                t_im = time.monotonic()

        bus.user_says(bed.service, 'ou_A', '结束会议')
        ws.close()
        self.assertIsNotNone(t_caption, 'captions never landed')
        self.assertIsNotNone(t_im, 'reply never delivered')
        table = {
            '说完→字幕落库': round((t_caption or t0) - t0, 2),
            '字幕→决策pending': round((t_decision or t_im or t0) - (t_caption or t0), 2),
            '决策→IM送达': round((t_im or t0) - (t_decision or t_im or t0), 2),
            '端到端(说完→IM)': round(t_im - t0, 2),
        }
        print('\n[latency-table]', table, flush=True)
        self.assertLess(t_im - t0, 30, f'fake chain too slow: {table}')


if __name__ == '__main__':
    unittest.main()
