import json
import socket
import struct
import threading
import unittest

from websockets.sync.server import serve

from prompt_studio.clients import RecorderArgs
from prompt_studio.ingest import IngestError, IngestSession


class FakeIngestServer:
    def __init__(self, greeting=None):
        self.greeting = greeting or {'type': 'started', 'stream_id': 'x',
                                     'acked_sequence': 0}
        self.start_message = None
        self.frames = 0
        self.end_sequence = None
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(('127.0.0.1', 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.server = serve(self._handle, sock=self.sock, open_timeout=5)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def _handle(self, ws):
        self.start_message = json.loads(ws.recv(timeout=5))
        ws.send(json.dumps(self.greeting))
        if self.greeting.get('type') != 'started':
            ws.close()
            return
        for raw in ws:
            if isinstance(raw, bytes):
                self.frames += 1
                if self.frames % 10 == 0:
                    sequence = struct.unpack('<I', raw[:4])[0]
                    ws.send(json.dumps({'type': 'ack', 'sequence': sequence}))
                continue
            message = json.loads(raw)
            if message.get('type') == 'stream_end':
                self.end_sequence = message['last_sequence']
                ws.send(json.dumps({'type': 'stopped',
                                    'acked_sequence': self.end_sequence}))
                return

    def stop(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.sock.close()

    @property
    def url(self):
        return f'ws://127.0.0.1:{self.port}/ingest'


class IngestTests(unittest.TestCase):
    def fake(self, greeting=None):
        server = FakeIngestServer(greeting)
        self.addCleanup(server.stop)
        return server

    def recorder(self, fake, session='s-1'):
        return RecorderArgs(session=session, token='tok', event='cal/evt',
                            url=fake.url)

    def test_start_frames_and_end(self):
        fake = self.fake()
        session = IngestSession(self.recorder(fake))
        self.assertEqual(session.start()['acked_sequence'], 0)
        self.assertEqual(fake.start_message, {
            'type': 'start', 'protocol': 1, 'session_id': 's-1',
            'stream_token': 'tok', 'source': 'mic', 'event': 'cal/evt',
            'first_sequence': 1, 'speaker': '本机'})
        for _ in range(3):
            session.send_turn()
        stopped = session.end()
        self.assertEqual(stopped['acked_sequence'], 3)
        self.assertEqual(fake.frames, 3)
        self.assertEqual(fake.end_sequence, 3)

    def test_rejected_start_raises(self):
        fake = self.fake({'type': 'error', 'code': 'bad_token', 'message': 'nope'})
        session = IngestSession(self.recorder(fake))
        with self.assertRaises(IngestError) as caught:
            session.start()
        self.assertIn('bad_token', str(caught.exception))

    def test_send_requires_start(self):
        fake = self.fake()
        session = IngestSession(self.recorder(fake))
        with self.assertRaises(IngestError):
            session.send_turn()

    def test_abort_is_quiet(self):
        fake = self.fake()
        session = IngestSession(self.recorder(fake))
        session.start()
        session.send_turn()
        session.abort()
        self.assertIsNone(fake.end_sequence)


if __name__ == '__main__':
    unittest.main()
