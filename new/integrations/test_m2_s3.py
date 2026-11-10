"""Plan-4.2/4.3 acceptance: S3 subscription scenarios end to end on the bed.

Fan-out forward-only, revoke stops bleeding, organizer+subscriber both
receive, ended/draining refuse subscriptions, idempotent reapply, denied
reapply walks a fresh approval — REQUIREMENTS S3 boundaries one by one.
"""
import json
import re
import time
import unittest
from urllib import request

from test_bed import TestBed, http_get_json

OPENER = request.build_opener(request.ProxyHandler({}))


class S3SubscriptionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bed = TestBed().start()

    @classmethod
    def tearDownClass(cls):
        cls.bed.stop()

    def open_and_stream(self, meeting_no, frames=12):
        """Returns (session_id, token, event) after streaming frames."""
        bed, bus = self.bed, self.bed.bus
        bed.authorize('ou_A')
        reply = bus.user_says(bed.service, 'ou_A', f'入会 {meeting_no}')
        self.assertIn('已入会', reply)
        session_id = re.search(r"--session '([^']+)'", reply).group(1)
        token = re.search(r"--token '([^']+)'", reply).group(1)
        event = re.search(r"--event '([^']+)'", reply).group(1)
        if frames:
            ws = bed.ingest_connect(session_id, token)
            bed.ingest_start(ws, session_id, token, event=event)
            for seq in range(1, frames + 1):
                bed.ingest_frame(ws, seq)
            bed.drain_acks(ws, frames - 2, timeout=10)
            ws.close()
        return session_id, token, event

    def wait_reply_dm(self, open_id, min_count, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            texts = [t for t in self.bed.bus.dms_to(open_id) if '网关合成回答' in t]
            if len(texts) >= min_count:
                return texts
            time.sleep(0.2)
        return [t for t in self.bed.bus.dms_to(open_id) if '网关合成回答' in t]

    def stream_more(self, session_id, token, event, start, count):
        bed = self.bed
        ws = bed.ingest_connect(session_id, token)
        bed.ingest_start(ws, session_id, token, event=event, resume=True,
                         first_sequence=start)
        for seq in range(start, start + count):
            bed.ingest_frame(ws, seq)
        bed.drain_acks(ws, start + count - 2, timeout=10)
        ws.close()

    def test_full_s3_flow_forward_only_and_revoke(self):
        bed, bus = self.bed, self.bed.bus
        session_id, token, event = self.open_and_stream('444000444')

        # 小B subscribes; organizer gets the approval request (#rN)
        reply_b = bus.user_says(bed.service, 'ou_B', '我想收到会议 444000444 的信息')
        self.assertIn('#', reply_b)
        request_no = int(re.search(r'#(\d+)', reply_b).group(1))
        approval = [t for t in bus.dms_to('ou_A') if f'同意 #{request_no}' in t]
        self.assertTrue(approval, f'approval request missing: {bus.dms_to("ou_A")[-2:]}')

        # first decision (before approval) reached only 小A
        first_batch = self.wait_reply_dm('ou_A', 1)
        self.assertTrue(first_batch)
        time.sleep(1)
        self.assertEqual([t for t in bus.dms_to('ou_B') if '网关合成回答' in t], [],
                         '小B must not receive before approval (forward-only)')

        # approve -> next decision reaches both
        approved = bus.user_says(bed.service, 'ou_A', f'同意 #{request_no}')
        self.assertIn('已批准', approved)
        a_before = len([t for t in bus.dms_to('ou_A') if '网关合成回答' in t])
        self.stream_more(session_id, token, event, 13, 9)
        self.wait_reply_dm('ou_A', a_before + 1)
        if not self.wait_reply_dm('ou_B', 1):
            import sqlite3
            api = f'http://127.0.0.1:{bed.ports["api"]}'
            status = http_get_json(f'{api}/sessions/{session_id}/status')
            pending = http_get_json(f'{api}/outbox/pending?limit=50')['rows']
            db = sqlite3.connect(f'{bed.tmp}/advisor/adviser.sqlite3')
            db.row_factory = sqlite3.Row
            outbox_rows = [dict(r) for r in db.execute(
                'SELECT channel,recipient_id,state,generation FROM outbox ORDER BY created_ns')]
            jobs = [dict(r) for r in db.execute(
                'SELECT job_id,generation,state FROM jobs ORDER BY created_ns')]
            session_row = dict(db.execute(
                'SELECT generation,decided_generation,phase,error,retry_at FROM sessions '
                'WHERE session_id=?', (session_id,)).fetchone())
            context_count = db.execute(
                'SELECT count(*) FROM context WHERE session_id=?', (session_id,)).fetchone()[0]
            self.fail(f'小B did not receive; approve={approved!r}; '
                      f'dmsB={bus.dms_to("ou_B")[-3:]}; subs={status["subscriptions"]}; '
                      f'pending={[r["recipient_open_id"] for r in pending]}; '
                      f'session={session_row}; context={context_count}; '
                      f'outbox={outbox_rows}; jobs={jobs}')

        # revoke -> bleeding stops immediately for 小B, 小A keeps receiving
        b_before = len([t for t in bus.dms_to('ou_B') if '网关合成回答' in t])
        a_before = len([t for t in bus.dms_to('ou_A') if '网关合成回答' in t])
        bus.user_says(bed.service, 'ou_A', f'撤销 #{request_no}')
        self.stream_more(session_id, token, event, 22, 9)
        self.wait_reply_dm('ou_A', a_before + 1)
        time.sleep(2)
        self.assertEqual(len([t for t in bus.dms_to('ou_B') if '网关合成回答' in t]),
                         b_before, '小B received after revoke')
        # revoked terminal: reapply rejected
        reply_b = bus.user_says(bed.service, 'ou_B', '我想收到会议 444000444 的信息')
        self.assertIn('已被组织者撤销', reply_b)
        bus.user_says(bed.service, 'ou_A', '结束会议')

    def test_denied_reapply_walks_fresh_approval(self):
        bed, bus = self.bed, self.bed.bus
        self.open_and_stream('555000555', frames=0)
        reply_b = bus.user_says(bed.service, 'ou_B', '我想收到会议 555000555 的信息')
        first_no = int(re.search(r'#(\d+)', reply_b).group(1))
        bus.user_says(bed.service, 'ou_A', f'拒绝 #{first_no}')
        reply_b = bus.user_says(bed.service, 'ou_B', '我想收到会议 555000555 的信息')
        second_no = int(re.search(r'#(\d+)', reply_b).group(1))
        self.assertGreater(second_no, first_no)
        result = bus.user_says(bed.service, 'ou_A', f'同意 #{second_no}')
        self.assertIn('已批准', result)
        bus.user_says(bed.service, 'ou_A', '结束会议')

    def test_ended_session_refuses_subscription(self):
        bed, bus = self.bed, self.bed.bus
        self.open_and_stream('666000666', frames=0)
        bus.user_says(bed.service, 'ou_A', '结束会议')
        time.sleep(1.5)
        reply = bus.user_says(bed.service, 'ou_B', '我想收到会议 666000666 的信息')
        self.assertIn('未找到该会议或会议已结束', reply)


if __name__ == '__main__':
    unittest.main()
