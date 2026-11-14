"""Join requires all organizer credentials and reports all missing links."""
import unittest

from test_bed import TestBed, http_get_json


class JoinCredentialTests(unittest.TestCase):
    def setUp(self):
        self.bed = TestBed(nsdp='stub', auto_personal_tokens=False).start()
        self.addCleanup(self.bed.stop)

    def join(self):
        return self.bed.bus.join_says(self.bed.service, 'ou_A', '入会 123456789')[1]

    def test_missing_links_then_submit_and_join(self):
        reply = self.join()
        for expected in ('飞书授权', 'NSDP token', 'Jira PAT', '/nsdp/submit', '/jira/submit'):
            self.assertIn(expected, reply)
        self.assertEqual(http_get_json(
            f'http://127.0.0.1:{self.bed.ports["api"]}/sessions?owner_open_id=ou_A')['sessions'], [])

        self.bed.authorize('ou_A')
        reply = self.join()
        self.assertNotIn('飞书授权', reply)
        self.assertIn('/nsdp/submit', reply)
        self.assertIn('/jira/submit', reply)

        self.bed.token_submit('ou_A', 'nsdp', 'stub-nsdp-token')
        reply = self.join()
        self.assertNotIn('/nsdp/submit', reply)
        self.assertIn('/jira/submit', reply)

        self.bed.token_submit('ou_A', 'jira', 'stub-jira-token')
        reply = self.join()
        self.assertIn('已入会', reply)
        self.assertIn('recorder', reply)
        self.assertFalse(self.bed.advisor_db_read(
            "SELECT 1 FROM session_materials WHERE body LIKE '%\"kind\":\"nsdp\"%'"))


if __name__ == '__main__':
    unittest.main()
