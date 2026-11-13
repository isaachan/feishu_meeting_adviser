"""M1/S6 e2e: NSDP organizer token gate over the real bed (auth HTTP included).

Chain: 小A 入会 blocked (submit link, every organizer command) -> real form
submission over HTTP -> feishu stub OAuth -> join succeeds with NSDP material
stored -> mid-session invalidation -> hold + organizer DM via outbox ->
re-submission -> resume. 小B's subscription surface never mentions NSDP.
"""
import time
import unittest

from test_bed import TestBed, http_get_json

OWNER = 'ou_A'
SUBSCRIBER = 'ou_B'


def _wait(predicate, timeout=20, interval=0.1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class NsdpEndToEndTests(unittest.TestCase):  # noqa: D101
    def setUp(self):
        self.bed = TestBed(nsdp='stub').start()
        self.bus = self.bed.bus

    def tearDown(self):
        self.bed.stop()

    def test_gate_submit_join_hold_resume(self):
        # 1) first organizer command hard-blocks with the submit link (async DM)
        _, reply = self.bus.join_says(self.bed.service, OWNER, '入会 123456789')
        self.assertIn('NSDP', reply)
        self.assertIn('/nsdp/submit?state=', reply)

        # 2) every further organizer command keeps returning the link
        reply = self.bus.user_says(self.bed.service, OWNER, '会议列表')
        self.assertIn('/nsdp/submit?state=', reply)

        # 3) organizer submits the token through the real HTTPS-shaped flow
        self.bed.nsdp_submit(OWNER, 'stub-nsdp-token-1')

        # 4) next join now proceeds to the (stub) feishu OAuth gate
        _, reply = self.bus.join_says(self.bed.service, OWNER, '入会 123456789')
        self.assertIn('授权', reply)
        self.assertNotIn('/nsdp/submit', reply)
        self.bed.authorize(OWNER)

        # 5) join succeeds; token/command red lines hold
        _, reply = self.bus.join_says(self.bed.service, OWNER, '入会 123456789')
        self.assertIn('已入会', reply)
        self.assertIn('recorder', reply)
        self.assertNotIn('stub-nsdp-token-1', reply)

        # 6) NSDP material landed in the session
        self.assertTrue(_wait(lambda: self.bed.advisor_db_read(
            "SELECT 1 FROM session_materials WHERE body LIKE '%\"kind\":\"nsdp\"%'")),
            'nsdp material missing')

        # 7) 小B's subscriber surface is untouched by the NSDP gate
        reply = self.bus.user_says(self.bed.service, SUBSCRIBER,
                                   '我想收到会议 123456789 的信息')
        self.assertIn('订阅', reply)
        self.assertNotIn('NSDP', reply)

        # 8) mid-session invalidation -> hold + organizer DM via outbox
        sessions = http_get_json(
            f'http://127.0.0.1:{self.bed.ports["api"]}/sessions?owner_open_id={OWNER}')
        session_id = sessions['sessions'][0]['session_id']
        self.bed.nsdp_invalidate(OWNER)
        status_url = f'http://127.0.0.1:{self.bed.ports["api"]}/sessions/{session_id}/status'
        self.assertTrue(_wait(lambda: http_get_json(status_url)['nsdp_hold']),
                        'session never went on hold')
        self.assertTrue(_wait(lambda: any('/nsdp/submit?state=' in text
                                          for text in self.bus.dms_to(OWNER))),
                        'organizer never nagged')

        # 9) re-submission resumes decisions
        self.bed.nsdp_submit(OWNER, 'stub-nsdp-token-2')
        self.assertTrue(_wait(lambda: not http_get_json(status_url)['nsdp_hold']),
                        'session never resumed')
        self.assertTrue(_wait(lambda: self.bed.advisor_db_read(
            "SELECT 1 FROM session_materials WHERE body LIKE '%桩%'")),
            'refreshed material missing')


if __name__ == '__main__':
    import unittest
    unittest.main()
