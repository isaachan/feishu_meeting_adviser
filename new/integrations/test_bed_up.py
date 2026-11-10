"""Plan-3.1 acceptance: the bed itself comes up and tears down cleanly."""
import unittest

from test_bed import TestBed


class BedTests(unittest.TestCase):
    def test_bring_up_and_teardown(self):
        bed = TestBed().start()
        self.addCleanup(bed.stop)
        from test_bed import http_get_json
        self.assertEqual(http_get_json(
            f'http://127.0.0.1:{bed.ports["auth"]}/healthz')['status'], 'ok')
        self.assertEqual(http_get_json(
            f'http://127.0.0.1:{bed.ports["gateway"]}/healthz')['status'], 'ok')
        self.assertEqual(http_get_json(
            f'http://127.0.0.1:{bed.ports["api"]}/healthz')['status'], 'ok')
        # gateway fake model reachable through the advisor-facing contract
        import hashlib, json as jsonlib
        from urllib import request
        opener = request.build_opener(request.ProxyHandler({}))
        canonical = jsonlib.dumps({'system': 'r', 'input': {}, 'output': 'decision-v1',
                                   'backend': 'fake', 'model': None},
                                  sort_keys=True, ensure_ascii=False, separators=(',', ':'))
        content_hash = hashlib.sha256(canonical.encode()).hexdigest()
        body = {'schema_version': 1, 'request_id': 'rm' + content_hash,
                'content_hash': content_hash, 'backend': 'fake', 'model': None,
                'system': 'r', 'input': {}, 'output': 'decision-v1',
                'deadline_seconds': 10}
        req = request.Request(f'http://127.0.0.1:{bed.ports["gateway"]}/v1/inference-jobs',
                              data=jsonlib.dumps(body).encode(), method='POST',
                              headers={'Content-Type': 'application/json'})
        with opener.open(req, timeout=10) as response:
            self.assertEqual(jsonlib.loads(response.read())['state'] in
                             ('queued', 'running', 'succeeded'), True)
        bed.stop()

    def test_fake_oauth_flow(self):
        bed = TestBed().start()
        self.addCleanup(bed.stop)
        bed.authorize('ou_A')  # asserts success page internally
        import json
        from urllib import request
        opener = request.build_opener(request.ProxyHandler({}))
        with opener.open(request.Request(
                f'http://127.0.0.1:{bed.ports["auth"]}/credentials/ou_A/token',
                method='GET'), timeout=5) as response:
            token = json.loads(response.read())
        self.assertEqual(token['access_token'], 'stub-ou_A')


if __name__ == '__main__':
    unittest.main()
