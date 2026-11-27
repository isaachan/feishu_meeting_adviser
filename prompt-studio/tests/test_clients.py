import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from prompt_studio.clients import (AdvisorClient, AuthClient, ClientError,
                                   RecorderArgs)


class StubHandler(BaseHTTPRequestHandler):
    requests = []

    def log_message(self, *args):
        pass

    def _body(self):
        length = int(self.headers.get('Content-Length', 0))
        return self.rfile.read(length) if length else b''

    def _reply(self, status, payload, content_type='application/json'):
        raw = payload.encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _record(self, body):
        self.requests.append({
            'method': self.command,
            'path': self.path,
            'idempotency': self.headers.get('X-Idempotency-Key'),
            'body': body,
        })

    def _handle(self):
        body = self._body()
        self._record(body)
        path = urlsplit(self.path).path
        if path == '/authorize/start':
            self._reply(200, json.dumps(
                {'authorize_url': 'http://localhost/oauth/callback?state=st1'}))
        elif path == '/oauth/callback':
            self._reply(200, '授权成功', 'text/html')
        elif path == '/token-submissions/start':
            provider = json.loads(body)['provider']
            self._reply(200, json.dumps(
                {'submit_url': f'http://localhost/{provider}/submit?state=st2'}))
        elif path in ('/nsdp/submit', '/jira/submit'):
            self._reply(200, '已保存', 'text/html')
        elif path == '/sessions':
            self._reply(200, json.dumps({
                'session_id': 's-1',
                'recorder_command_args': {
                    'session': 's-1', 'token': 'tok', 'event': 'cal/evt',
                    'url': 'ws://localhost/ingest'}}))
        elif path == '/sessions/s-1/status':
            self._reply(200, json.dumps({'session_id': 's-1', 'phase': 'running'}))
        elif path == '/admin/sessions/s-1/transcript':
            self._reply(200, json.dumps({'records': []}))
        elif path == '/admin/sessions/s-1/timings':
            self._reply(200, json.dumps({'jobs': []}))
        elif path == '/admin/sessions/s-1/end':
            self._reply(200, json.dumps({'ended': True}))
        elif path == '/bad':
            self._reply(409, json.dumps(
                {'error': {'code': 'authorization_required', 'message': 'nope'}}))
        else:
            self._reply(404, json.dumps(
                {'error': {'code': 'not_found', 'message': path}}))


for method in ('do_GET', 'do_POST'):
    setattr(StubHandler, method, StubHandler._handle)


class ClientTests(unittest.TestCase):
    def setUp(self):
        StubHandler.requests = []
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), StubHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def auth(self):
        return AuthClient(self.base)

    def advisor(self):
        return AdvisorClient(self.base)

    def test_authorize_uses_stub_callback(self):
        self.auth().authorize('ou_1')
        callback = [item for item in StubHandler.requests
                    if urlsplit(item['path']).path == '/oauth/callback'][0]
        query = parse_qs(urlsplit(callback['path']).query)
        self.assertEqual(query['code'], ['ou_1'])
        self.assertEqual(query['state'], ['st1'])

    def test_provider_token_goes_in_form(self):
        self.auth().submit_provider_token('ou_1', 'nsdp', 'secret-token')
        submit = [item for item in StubHandler.requests
                  if urlsplit(item['path']).path == '/nsdp/submit'][0]
        self.assertNotIn('secret-token', submit['path'])
        self.assertEqual(parse_qs(submit['body'].decode()), {'token': ['secret-token']})

    def test_create_session_sends_idempotency_and_meeting(self):
        response = self.advisor().create_session('ou_1', '123456789')
        self.assertEqual(response['session_id'], 's-1')
        sent = [item for item in StubHandler.requests
                if urlsplit(item['path']).path == '/sessions'][0]
        self.assertTrue(sent['idempotency'])
        self.assertEqual(json.loads(sent['body'])['meeting_ref'], {'no': '123456789'})

    def test_reads_and_end(self):
        client = self.advisor()
        self.assertEqual(client.status('s-1')['phase'], 'running')
        self.assertEqual(client.transcript('s-1')['records'], [])
        self.assertEqual(client.timings('s-1')['jobs'], [])
        self.assertTrue(client.end_session('s-1')['ended'])

    def test_error_maps_code_and_status(self):
        with self.assertRaises(ClientError) as caught:
            self.advisor()._request('GET', '/bad')
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(caught.exception.code, 'authorization_required')

    def test_recorder_args(self):
        args = AdvisorClient.recorder_args({
            'recorder_command_args': {
                'session': 's-1', 'token': 'tok', 'event': 'cal/evt',
                'url': 'ws://x/ingest'}})
        self.assertEqual(args, RecorderArgs('s-1', 'tok', 'cal/evt',
                                            'ws://x/ingest'))
        with self.assertRaises(ClientError):
            AdvisorClient.recorder_args({})


if __name__ == '__main__':
    unittest.main()
