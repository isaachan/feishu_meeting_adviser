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
                              stop_event=threading.Event(),
                              metadata={'owner_open_id': 'ou_A'})

    def test_provider_config_is_forwarded_to_the_isolated_instance(self):
        spec = {'synthetic': {'npm': '@ai-sdk/openai-compatible',
                              'api': 'https://example.invalid/v1',
                              'models': {'m': {}}}}
        provider_config = self.root / 'provider.json'
        provider_config.write_text(json.dumps(spec))
        backend = OpenCodeBackend(executable='opencode', model='synthetic/m',
                                  auth_path=self.auth, runtime_base=self.root / 'runtime',
                                  provider_config=provider_config)
        backend.runtime = self.root / 'runtime' / 'run-test'
        backend.runtime.mkdir(parents=True)
        backend.password = 'synthetic-password'
        backend.owner_open_id = 'ou_A'
        config = json.loads(backend._environment()['OPENCODE_CONFIG_CONTENT'])
        self.assertEqual(config['provider'], spec)
        self.assertEqual(config['enabled_providers'], ['synthetic'])

    def test_nsdp_mcp_receives_trusted_cache_path(self):
        backend = self.backend('reply')
        backend.nsdp_cache_db = self.root / 'catalog.sqlite3'
        backend.runtime = self.root / 'runtime' / 'run-test'
        backend.runtime.mkdir(parents=True)
        backend.password = 'synthetic-password'
        backend.owner_open_id = 'ou_A'
        backend.deadline = 12345.0
        config = json.loads(backend._environment()['OPENCODE_CONFIG_CONTENT'])
        command = config['mcp']['meeting_nsdp']['command']
        self.assertIn('--cache-db', command)
        self.assertIn(str(backend.nsdp_cache_db), command)
        self.assertIn('--deadline-monotonic', command)

    def test_nsdp_stage_file_is_exposed_in_task_diagnostics(self):
        backend = self.backend('reply')
        backend.runtime = self.root / 'runtime' / 'run-test'
        backend.runtime.mkdir(parents=True)
        (backend.runtime / 'tool-timings.jsonl').write_text(
            json.dumps({'event': 'nsdp_stage', 'call_id': 'abc123',
                        'stage': 'noam_start', 'at_ns': 123}) + '\n' +
            json.dumps({'tool': 'meeting_nsdp_lookup_release', 'status': 'ok',
                        'ms': 42, 'cache_hit': False, 'auth_ms': 2,
                        'noam_ms': 40}) + '\n')
        backend._read_tool_timing_file()
        self.assertEqual(backend.diagnostics['nsdp_stages'][0]['stage'], 'noam_start')
        self.assertEqual(backend.tool_timings[0]['cache_hit'], False)
        self.assertEqual(backend.tool_timings[0]['noam_ms'], 40)

    def test_provider_config_must_define_the_selected_provider(self):
        provider_config = self.root / 'provider.json'
        provider_config.write_text(json.dumps({'other': {}}))
        backend = OpenCodeBackend(executable='opencode', model='synthetic/m',
                                  auth_path=self.auth, runtime_base=self.root / 'runtime',
                                  provider_config=provider_config)
        backend.runtime = self.root / 'runtime' / 'run-test'
        backend.runtime.mkdir(parents=True)
        backend.password = 'synthetic-password'
        backend.owner_open_id = 'ou_A'
        with self.assertRaises(BackendError):
            backend._environment()

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

    def test_jira_auth_failure_is_recorded(self):
        backend = self.backend('jira-unauthorized')
        backend.decide('role', {'fresh_input_keys': ['s:1']}, deadline_seconds=8,
                       stop_event=threading.Event(), metadata={'owner_open_id': 'ou_A'})
        self.assertEqual(backend.tool_events, {'jira_authorization_required'})

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

    def test_stage_timings_are_recorded_for_the_sequential_chain(self):
        backend = self.backend('reply')
        self.decide(backend)
        stages = backend.stage_timings
        for name in ('process_start', 'verify_config', 'verify_agent', 'verify_mcp',
                     'verify_provider', 'verify_tool', 'session_prepare',
                     'prompt_submit', 'prompt_visible', 'decision_wait',
                     'reply_validate'):
            self.assertIn(name + '_ms', stages)
            self.assertIsInstance(stages[name + '_ms'], int)
        self.assertIsNone(backend.failed_stage)
        self.assertEqual(backend.attempts, 1)

    def test_skill_call_is_observed_through_polling(self):
        backend = self.backend('skill-tool')
        backend.decide('role', {'fresh_input_keys': ['s:1']}, deadline_seconds=8,
                       stop_event=threading.Event(),
                       metadata={'owner_open_id': 'ou_A'})
        skill = [item for item in backend.tool_timings if item['tool'] == 'skill']
        self.assertEqual(len(skill), 1)
        self.assertEqual(skill[0]['status'], 'ok')
        self.assertEqual(skill[0]['source'], 'poll')

    def test_opencode_trace_records_progress_without_content(self):
        backend = self.backend('progress-timeline')
        self.decide(backend)
        trace = backend.diagnostics['opencode_trace']
        kinds = [event['kind'] for event in trace['events']]
        self.assertLess(kinds.index('reasoning_seen'), kinds.index('tool_running'))
        self.assertLess(kinds.index('tool_running'), kinds.index('tool_completed'))
        self.assertLess(kinds.index('tool_completed'), kinds.index('assistant_completed'))
        self.assertEqual(trace['active_tools'], [])
        self.assertEqual(trace['source'], 'message_poll')
        self.assertNotIn('private thought', json.dumps(trace))
        self.assertTrue(backend.diagnostics['wait_observation']['completed'])

    def test_timeout_keeps_completed_stages_and_failed_stage(self):
        backend = self.backend('timeout')
        with self.assertRaises(BackendError) as caught:
            self.decide(backend, deadline=1)
        self.assertEqual(caught.exception.code, 'execution_timeout')
        for name in ('process_start_ms', 'verify_config_ms', 'verify_agent_ms',
                     'verify_mcp_ms', 'verify_provider_ms', 'verify_tool_ms',
                     'session_prepare_ms', 'prompt_submit_ms', 'decision_wait_ms'):
            self.assertIn(name, backend.stage_timings)
        self.assertEqual(backend.failed_stage, 'decision_wait')

    def test_timeout_records_inflight_tool_and_child_stderr(self):
        backend = self.backend('hang-tool')
        with self.assertRaises(BackendError) as caught:
            self.decide(backend, deadline=4)
        self.assertEqual(caught.exception.code, 'execution_timeout')
        observation = backend.diagnostics['wait_observation']
        self.assertTrue(observation['user_message_seen'])
        self.assertFalse(observation['completed'])
        self.assertEqual(observation['parts'],
                         [{'type': 'tool', 'tool': 'meeting_nsdp_lookup_release',
                           'status': 'running'}])
        trace = backend.diagnostics['opencode_trace']
        self.assertIn('tool_running', [event['kind'] for event in trace['events']])
        self.assertEqual(trace['active_tools'][0]['tool'],
                         'meeting_nsdp_lookup_release')
        self.assertGreater(trace['active_tools'][0]['running_ms'], 0)
        self.assertIn('synthetic provider stalled', backend.diagnostics['stderr_tail'])
        self.assertGreater(backend.diagnostics['stderr_bytes'], 0)


if __name__ == '__main__':
    unittest.main()
