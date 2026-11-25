"""SM-13 acceptance: state-machine v2 end-to-end on the fake bed.

Covers the matrix rows that need the real bot+advisor+ingest chain:
instance claiming (first-wins), single-claim across sessions, duplicate
join idempotence, and the absolute drain deadline with a dead bot.
Occurrence identity / paused transitions / STT isolation are covered by
advisor unit suites (test_domain, test_observation, test_ingest).
"""
import json
import time
import unittest

from test_bed import TestBed, http_get_json, wait_until


class StateMatrixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bed = TestBed(extra_advisor_args=[
            '--drain-deadline-s', '3',
            '--reply-validity-s', '3600',
        ]).start()

    @classmethod
    def tearDownClass(cls):
        cls.bed.stop()

    def setUp(self):
        self.users = []

    def tearDown(self):
        bus, bed = self.bed.bus, self.bed
        bed.service.start_polling()
        for user, meeting in self.users:
            try:
                bus.user_says(bed.service, user, f'结束会议 {meeting}')
            except Exception:
                pass
        wait_until(lambda: all(
            s['phase'] != 'running' for s in self.bed.admin_sessions()), timeout=10)

    def track(self, user, meeting):
        self.users.append((user, meeting))
        return user, meeting

    def test_first_join_wins_and_second_owner_gets_claimed(self):
        bed, bus = self.bed, self.bed.bus
        bed.authorize('ou_A')
        bed.authorize('ou_C')
        self.track('ou_A', '244000111')
        _ack, first = bus.join_says(bed.service, 'ou_A', '入会 244000111')
        self.assertIn('已入会', first)
        _ack, second = bus.join_says(bed.service, 'ou_C', '入会 244000111')
        self.assertIn('已由其他人开启', second)
        # same owner re-join stays idempotent
        _ack, again = bus.join_says(bed.service, 'ou_A', '入会 244000111')
        self.assertIn('已入会', again)
        owners = [s['owner_open_id'] for s in self.bed.admin_sessions()
                  if s['meeting_no'] == '244000111']
        self.assertEqual(owners, ['ou_A'])

    def test_single_claim_blocks_second_meeting(self):
        bed, bus = self.bed, self.bed.bus
        bed.authorize('ou_D')
        self.track('ou_D', '244000222')
        _ack, reply = bus.join_says(bed.service, 'ou_D', '入会 244000222')
        self.assertIn('已入会', reply)
        _ack, conflict = bus.join_says(bed.service, 'ou_D', '入会 244000333')
        self.assertIn('请先结束或退出', conflict)
        # 结束后可再建（不同实例）——占位只到 ended 为止
        bus.user_says(bed.service, 'ou_D', '结束会议 244000222')
        self.assertTrue(wait_until(lambda: all(
            s['phase'] == 'ended' for s in self.bed.admin_sessions()
            if s['meeting_no'] == '244000222')), 'session never drained to ended')
        _ack, again = bus.join_says(bed.service, 'ou_D', '入会 244000333')
        self.assertIn('已入会', again)

    def test_reopen_after_manual_end_creates_new_session(self):
        bed, bus = self.bed, self.bed.bus
        bed.authorize('ou_E')
        self.track('ou_E', '244000555')
        _ack, first = bus.join_says(bed.service, 'ou_E', '入会 244000555')
        self.assertIn('已入会', first)
        first_sid, first_token, _first_event = bed.recorder_args('ou_E', '244000555')
        bus.user_says(bed.service, 'ou_E', '结束会议 244000555')
        self.assertTrue(wait_until(lambda: any(
            s['session_id'] == first_sid and s['phase'] == 'ended'
            for s in self.bed.admin_sessions()), timeout=20), 'first session never ended')

        _ack, second = bus.join_says(bed.service, 'ou_E', '入会 244000555')
        self.assertIn('已入会', second)
        second_sid, second_token, second_event = bed.recorder_args('ou_E', '244000555')
        self.assertNotEqual(second_sid, first_sid)

        old = bed.ingest_connect(first_sid, first_token)
        old.send(json.dumps({'type': 'start', 'protocol': 1, 'session_id': first_sid,
                             'stream_token': first_token, 'source': 'loopback',
                             'event': _first_event, 'resume': False, 'first_sequence': 1,
                             'wall_clock_at': '2026-11-10T14:20:00+08:00'}))
        error = json.loads(old.recv(timeout=10))
        self.assertEqual((error['type'], error['code']), ('error', 'session_ended'))
        old.close()

        ws = bed.ingest_connect(second_sid, second_token)
        bed.ingest_start(ws, second_sid, second_token, event=second_event)
        bed.ingest_frame(ws, 1)
        bed.drain_acks(ws, 1, timeout=10)
        ws.close()

    def test_drain_deadline_forces_ended_with_dead_bot(self):
        bed, bus = self.bed, self.bed.bus
        bed.authorize('ou_H')
        self.track('ou_H', '244000444')
        _ack, reply = bus.join_says(bed.service, 'ou_H', '入会 244000444')
        session_id, token, event = bed.recorder_args('ou_H', '244000444')
        # kill the bot poller FIRST so generated replies stay pending
        bed.service.stop_polling()
        ws = bed.ingest_connect(session_id, token)
        bed.ingest_start(ws, session_id, token, event=event)
        for seq in range(1, 16):
            bed.ingest_frame(ws, seq)
        bed.drain_acks(ws, 14, timeout=10)
        ws.close()
        pending = []
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline and not pending:
            pending = http_get_json(
                f'http://127.0.0.1:{bed.ports["api"]}/outbox/pending?limit=10')['rows']
            time.sleep(0.2)
        self.assertTrue(pending, 'no pending reply accumulated')
        bus.user_says(bed.service, 'ou_H', '结束会议 244000444')
        # drain deadline (3s) forces ended even though IM cannot send
        forced = wait_until(lambda: next(
            s['phase'] == 'ended' for s in self.bed.admin_sessions()
            if s['session_id'] == session_id), timeout=20)
        self.assertTrue(forced)
        row = next(s for s in self.bed.admin_sessions() if s['session_id'] == session_id)
        self.assertEqual(row['drain_forced_reason'], 'drain_deadline')
        states = {r[0] for r in bed.advisor_db_read(
            "SELECT state FROM outbox WHERE session_id=? AND channel='im'",
            (session_id,))}
        self.assertTrue(states <= {'cancelled', 'uncertain'}, states)
        self.assertNotIn('sent', states)  # never faked
        # restart polling: backlog was cancelled, nothing stale is delivered
        bed.service.start_polling()
        time.sleep(1.0)
        states2 = {r[0] for r in bed.advisor_db_read(
            "SELECT state FROM outbox WHERE session_id=? AND channel='im'",
            (session_id,))}
        self.assertEqual(states2, states)


if __name__ == '__main__':
    unittest.main()
