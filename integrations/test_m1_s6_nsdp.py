"""NSDP authorization no longer gates the meeting or prefetches material."""
import unittest

from test_bed import TestBed, http_get_json

OWNER = 'ou_A'
SUBSCRIBER = 'ou_B'


class NsdpEndToEndTests(unittest.TestCase):  # noqa: D101
    def setUp(self):
        self.bed = TestBed(nsdp='stub').start()
        self.bus = self.bed.bus

    def tearDown(self):
        self.bed.stop()

    def test_missing_or_invalid_nsdp_does_not_block_meeting(self):
        # Feishu authorization is still needed, independently of NSDP.
        _, reply = self.bus.join_says(self.bed.service, OWNER, '入会 123456789')
        self.assertIn('授权', reply)
        self.assertNotIn('/nsdp/submit', reply)
        self.bed.authorize(OWNER)
        _, reply = self.bus.join_says(self.bed.service, OWNER, '入会 123456789')
        self.assertIn('已入会', reply)
        self.assertIn('recorder', reply)
        self.assertFalse(self.bed.advisor_db_read(
            "SELECT 1 FROM session_materials WHERE body LIKE '%\"kind\":\"nsdp\"%'"))
        reply = self.bus.user_says(self.bed.service, SUBSCRIBER,
                                   '我想收到会议 123456789 的信息')
        self.assertIn('订阅', reply)
        self.assertNotIn('NSDP', reply)
        sessions = http_get_json(
            f'http://127.0.0.1:{self.bed.ports["api"]}/sessions?owner_open_id={OWNER}')
        session_id = sessions['sessions'][0]['session_id']
        self.bed.nsdp_submit(OWNER, 'stub-nsdp-token-1')
        self.bed.nsdp_invalidate(OWNER)
        status_url = f'http://127.0.0.1:{self.bed.ports["api"]}/sessions/{session_id}/status'
        self.assertFalse(http_get_json(status_url)['nsdp_hold'])


if __name__ == '__main__':
    import unittest
    unittest.main()
