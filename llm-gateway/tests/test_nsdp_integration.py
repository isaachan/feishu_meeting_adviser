"""Synthetic end-to-end NSDP tool test; optionally exercise real OpenCode."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from llm_gateway.backends.opencode import OpenCodeBackend


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
