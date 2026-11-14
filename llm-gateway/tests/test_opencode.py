"""Gateway-side OpenCode adapter against a synthetic HTTP peer."""
from pathlib import Path
import json
import tempfile
import threading
import unittest

from llm_gateway.backends.base import BackendError
from llm_gateway.backends.opencode import OpenCodeBackend

PEER = Path(__file__).parent / 'helpers' / 'fake_opencode_server.py'


class OpenCodeBackendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.auth = self.root / 'auth.json'
        self.auth.write_text('{"zai-coding-plan": {"type": "api", "key": "synthetic-key"}}')
        self.auth.chmod(0o600)

    def backend(self, mode, capture='capture'):
        return OpenCodeBackend(
            executable='opencode', model='zai-coding-plan/glm-5.3',
            auth_path=self.auth, runtime_base=self.root / 'runtime',
            command=[__import__('sys').executable, str(PEER), mode, str(self.root / capture)])

    def decide(self, backend, deadline=8):
        return backend.decide('会议角色', {'input': 1, 'fresh_input_keys': ['s:1']},
                              deadline_seconds=deadline,
                              stop_event=threading.Event(), metadata={})

    def test_reply_decision_passes_verification_and_schema(self):
        decision = self.decide(self.backend('reply'))
        self.assertEqual((decision['reply'], decision['reply_sentence_keys']),
                         ('SYNTHETIC GLM REPLY', ['s:1']))

    def test_skill_tool_call_followed_by_final_decision(self):
        backend = self.backend('skill-tool')
        decision = backend.decide('role', {'fresh_input_keys': ['s:1']},
                                  deadline_seconds=8, stop_event=threading.Event(),
                                  metadata={'owner_open_id': 'ou_A'})
        self.assertIsNone(decision['reply'])

    def test_catalog_is_supplied_as_reference_context(self):
        backend = self.backend('skill-tool')
        catalog = {'source': 'nsdp', 'fetched_at': 1,
                   'data': {'tenants': ['NIO'], 'platforms': []}}
        backend.decide('role', {'fresh_input_keys': ['s:1']}, deadline_seconds=8,
                       stop_event=threading.Event(), metadata={'owner_open_id': 'ou_A',
                                                               '_gateway_context': catalog})
        requests = [json.loads(line) for line in (self.root / 'capture').read_text().splitlines()]
        prompt = next(item['payload'] for item in requests
                      if item.get('path', '').endswith('/prompt_async'))
        supplied = json.loads(prompt['parts'][0]['text'])
        self.assertEqual(supplied['reference_context']['nsdp_catalog'], catalog)

    def test_nsdp_auth_failure_is_recorded_independently_of_model_decision(self):
        backend = self.backend('nsdp-unauthorized')
        backend.decide('role', {'fresh_input_keys': ['s:1']}, deadline_seconds=8,
                       stop_event=threading.Event(), metadata={'owner_open_id': 'ou_A'})
        self.assertEqual(backend.tool_events, {'nsdp_authorization_required'})

    def test_invalid_output_and_tool_requests_map_to_output_invalid(self):
        for mode in ('invalid', 'tool'):
            with self.subTest(mode=mode):
                with self.assertRaises(BackendError) as caught:
                    self.decide(self.backend(mode))
                self.assertEqual(caught.exception.code, 'output_invalid')

    def test_auth_and_disconnect_failures_map_to_backend_unavailable(self):
        for mode in ('auth-missing', 'disconnect-always'):
            with self.subTest(mode=mode):
                with self.assertRaises(BackendError) as caught:
                    self.decide(self.backend(mode))
                self.assertEqual(caught.exception.code, 'backend_unavailable')

    def test_single_transport_disconnect_is_retried_once(self):
        decision = self.decide(self.backend('disconnect-once-verify'))
        self.assertIsNone(decision['reply'])

    def test_slow_provider_catalog_stays_within_the_request_budget(self):
        decision = self.decide(self.backend('slow-provider'))
        self.assertIsNone(decision['reply'])

    def test_deadline_overrun_maps_to_execution_timeout(self):
        with self.assertRaises(BackendError) as caught:
            self.decide(self.backend('timeout'), deadline=1)
        self.assertEqual(caught.exception.code, 'execution_timeout')

    def test_runtime_is_cleaned_and_reused_between_decisions(self):
        backend = self.backend('reply')
        self.decide(backend)
        self.decide(backend)
        runs = list((self.root / 'runtime' / 'opencode').glob('run-*'))
        self.assertEqual(runs, [])


if __name__ == '__main__':
    unittest.main()
