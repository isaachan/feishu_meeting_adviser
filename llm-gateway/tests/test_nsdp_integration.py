"""Synthetic end-to-end NSDP tool test; optionally exercise real OpenCode."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from llm_gateway.backends.opencode import OpenCodeBackend
from llm_gateway.catalog import MeetingCatalogs
from llm_gateway.nsdp_query import NsdpQuery
from llm_gateway.nsdp_tool_server import credential_provider


class AuthHandler(BaseHTTPRequestHandler):
    paths = []
    status = 200

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.paths.append(self.path)
        body = json.dumps({'token': 'synthetic-token'}).encode()
        self.send_response(self.status)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class NsdpIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.noam = self.root / 'noam'
        self.noam.write_text('#!/usr/bin/env python3\n'
                             'import os, sys\n'
                             'assert os.environ.get("NSDP_TOKEN") == "synthetic-token"\n'
                             'if sys.argv[1:] == ["nsdp", "info"]:\n'
                             ' print(\'{"tenants":["NIO"],"platforms":[{"platform_name":"NT3",'
                             '"versions":["BL1.5.0"]}]}\')\n'
                             ' sys.exit(0)\n'
                             'assert sys.argv[1:] == ["nsdp", "releases", "--release", "BL1.5.0"]\n'
                             'print("BL1.5.0 G4 2026-12-01")\n')
        self.noam.chmod(0o700)
        AuthHandler.paths = []
        AuthHandler.status = 200
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), AuthHandler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.auth_url = f'http://127.0.0.1:{self.server.server_port}'

    def test_mcp_subprocess_queries_auth_and_noam(self):
        process = subprocess.Popen(
            [sys.executable, '-m', 'llm_gateway.nsdp_tool_server', '--owner', 'ou_A',
             '--auth-url', self.auth_url, '--noam-bin', str(self.noam)],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        message = {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call', 'params': {
            'name': 'lookup_release', 'arguments': {'release': 'BL1.5.0'}}}
        stdout, stderr = process.communicate(json.dumps(message) + '\n', timeout=10)
        self.assertEqual(process.returncode, 0, stderr)
        result = json.loads(stdout)['result']
        self.assertFalse(result['isError'])
        self.assertIn('2026-12-01', result['content'][0]['text'])
        self.assertNotIn('synthetic-token', stdout + stderr)
        self.assertEqual(AuthHandler.paths, ['/credentials/ou_A/token?provider=nsdp'])

    @unittest.skipUnless(os.environ.get('LIVE_OPENCODE_TEST') == '1', 'requires real OpenCode model')
    def test_real_opencode_calls_scoped_tool(self):
        backend = OpenCodeBackend(executable='opencode', model='zai-coding-plan/glm-5.3',
                                  auth_path=None, runtime_base=self.root,
                                  auth_url=self.auth_url, noam_bin=str(self.noam))
        decision = backend.decide(
            'Synthetic test: call meeting_nsdp_lookup_release for BL1.5.0, '
            'then return null reply.',
            {'generation': 1, 'captions': [], 'fresh_input_keys': [], 'reply_allowed_keys': []},
            deadline_seconds=90, stop_event=threading.Event(), metadata={'owner_open_id': 'ou_A'})
        self.assertIsNone(decision['reply'])
        self.assertEqual(AuthHandler.paths, ['/credentials/ou_A/token?provider=nsdp'])

    @unittest.skipUnless(os.environ.get('LIVE_OPENCODE_TEST') == '1', 'requires real OpenCode model')
    def test_real_opencode_selects_version_skill_and_checks_gate(self):
        backend = OpenCodeBackend(executable='opencode', model='zai-coding-plan/glm-5.3',
                                  auth_path=None, runtime_base=self.root,
                                  auth_url=self.auth_url, noam_bin=str(self.noam))
        original_result = backend._result
        calls = []

        def observe(message, message_id):
            for part in message.get('parts', []):
                if part.get('type') == 'tool':
                    calls.append((part.get('tool'), part.get('state', {}).get('input')))
            return original_result(message, message_id)

        backend._result = observe
        decision = backend.decide(
            'meeting-snapshot-v1',
            {'generation': 1,
             'captions': [{'sentence_key': 's:1',
                           'text': '会参谋，请核对 BL1.5.0 的 G4 是不是 2026年12月2日？'}],
             'fresh_input_keys': ['s:1'], 'reply_allowed_keys': ['s:1'],
             'pending_reply': None, 'sent_replies': []},
            deadline_seconds=90, stop_event=threading.Event(), metadata={'owner_open_id': 'ou_A'})
        self.assertTrue(any(name == 'skill' and isinstance(args, dict)
                            and args.get('name') == 'nsdp-version' for name, args in calls), calls)
        self.assertTrue(any(name == 'meeting_nsdp_lookup_release' for name, _ in calls), calls)
        self.assertEqual(AuthHandler.paths, ['/credentials/ou_A/token?provider=nsdp'])
        self.assertIsNotNone(decision['reply'])
        self.assertEqual(decision['reply_sentence_keys'], ['s:1'])

    @unittest.skipUnless(os.environ.get('LIVE_OPENCODE_TEST') == '1', 'requires real OpenCode model')
    def test_unaddressed_version_question_and_incorrect_claim(self):
        for text in ('BL1.5.0 的 G4 是哪一天？',
                     'BL1.5.0 的 G4 是 2026 年 12 月 2 日。'):
            with self.subTest(text=text):
                AuthHandler.paths = []
                backend = OpenCodeBackend(executable='opencode', model='zai-coding-plan/glm-5.3',
                                          auth_path=None, runtime_base=self.root,
                                          auth_url=self.auth_url, noam_bin=str(self.noam))
                original_result = backend._result
                outputs = []

                def observe(message, message_id):
                    for part in message.get('parts', []):
                        state = part.get('state', {})
                        if (part.get('type') == 'tool'
                                and part.get('tool') == 'meeting_nsdp_lookup_release'
                                and state.get('status') == 'completed'):
                            outputs.append(state.get('output', ''))
                    return original_result(message, message_id)

                backend._result = observe
                decision = backend.decide(
                    'meeting-snapshot-v1',
                    {'generation': 1, 'captions': [{'sentence_key': 's:1', 'text': text}],
                     'fresh_input_keys': ['s:1'], 'reply_allowed_keys': ['s:1'],
                     'pending_reply': None, 'sent_replies': []},
                    deadline_seconds=90, stop_event=threading.Event(),
                    metadata={'owner_open_id': 'ou_A'})
                self.assertIn('/credentials/ou_A/token?provider=nsdp', AuthHandler.paths)
                self.assertTrue(any('2026-12-01' in str(output) for output in outputs), outputs)
                self.assertIsNotNone(decision['reply'])
                self.assertEqual(decision['reply_sentence_keys'], ['s:1'])
                self.assertRegex(decision['reply'], re.compile(r'(?:12\s*[月./-]\s*0?1|十二月一)'))

    @unittest.skipUnless(os.environ.get('LIVE_OPENCODE_TEST') == '1', 'requires real OpenCode model')
    def test_live_catalog_helps_identify_asr_version(self):
        query = NsdpQuery(credential_provider(self.auth_url), noam_bin=str(self.noam))
        catalogs = MeetingCatalogs(self.root / 'catalog.sqlite3', query)
        self.addCleanup(catalogs.close)
        catalogs.prepare('s-a', 'ou_A')
        deadline = time.monotonic() + 10
        while catalogs.get('s-a', 'ou_A') is None and time.monotonic() < deadline:
            time.sleep(0.05)
        catalog = catalogs.get('s-a', 'ou_A')
        self.assertIsNotNone(catalog)
        backend = OpenCodeBackend(executable='opencode', model='zai-coding-plan/glm-5.3',
                                  auth_path=None, runtime_base=self.root,
                                  auth_url=self.auth_url, noam_bin=str(self.noam))
        original_result = backend._result
        verified = []

        def observe(message, message_id):
            for part in message.get('parts', []):
                state = part.get('state', {})
                if (part.get('type') == 'tool' and part.get('tool') == 'meeting_nsdp_lookup_release'
                        and state.get('status') == 'completed'):
                    verified.append((state.get('input'), state.get('output')))
            return original_result(message, message_id)

        backend._result = observe
        decision = backend.decide(
            'meeting-snapshot-v1',
            {'generation': 1,
             'captions': [{'sentence_key': 's:1',
                           'text': '会参谋，版本比艾勒一五零的 G4 是不是 2026年12月2日？'}],
             'fresh_input_keys': ['s:1'], 'reply_allowed_keys': ['s:1'],
             'pending_reply': None, 'sent_replies': []},
            deadline_seconds=90, stop_event=threading.Event(),
            metadata={'owner_open_id': 'ou_A', '_gateway_context': catalog})
        self.assertIsNotNone(decision['reply'])
        self.assertEqual(decision['reply_sentence_keys'], ['s:1'])
        self.assertGreaterEqual(len(AuthHandler.paths), 2)  # info and version lookup
        self.assertTrue(any(isinstance(args, dict) and args.get('release') == 'BL1.5.0'
                            and '2026-12-01' in str(output) for args, output in verified), verified)

    @unittest.skipUnless(os.environ.get('LIVE_OPENCODE_TEST') == '1', 'requires real OpenCode model')
    def test_real_opencode_reports_missing_authorization(self):
        AuthHandler.status = 404
        backend = OpenCodeBackend(executable='opencode', model='zai-coding-plan/glm-5.3',
                                  auth_path=None, runtime_base=self.root,
                                  auth_url=self.auth_url, noam_bin=str(self.noam))
        backend.decide('Synthetic test: call meeting_nsdp_lookup_release for BL1.5.0, '
                       'then return null reply.',
                       {'generation': 1, 'captions': [], 'fresh_input_keys': [],
                        'reply_allowed_keys': []}, deadline_seconds=90,
                       stop_event=threading.Event(), metadata={'owner_open_id': 'ou_A'})
        self.assertIn('nsdp_authorization_required', backend.tool_events)


if __name__ == '__main__':
    unittest.main()
