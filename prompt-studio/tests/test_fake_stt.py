import json
import unittest

from websockets.sync.client import connect

from prompt_studio.fake_stt import FakeSttServer


class FakeSttTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeSttServer().start()
        self.addCleanup(self.server.stop)

    def client(self):
        return connect(self.server.url, open_timeout=5, proxy=None)

    @staticmethod
    def start(ws, source_id='s-1:mic', protocol=2):
        ws.send(json.dumps({'type': 'start', 'protocol': protocol,
                            'source_id': source_id, 'rate': 16000,
                            'channels': 1}))
        return json.loads(ws.recv(timeout=5))

    def test_scripted_finals_follow_frames(self):
        self.server.register('s-1', 'mic', ['第一句', '第二句'])
        with self.client() as ws:
            self.assertEqual(self.start(ws)['type'], 'started')
            ws.send(b'\x00\x00' * 100)
            first = json.loads(ws.recv(timeout=5))
            self.assertEqual(first, {'type': 'final', 'text': '第一句',
                                      'start_offset': 0, 'end_offset': 100})
            ws.send(b'\x00\x00' * 100)
            second = json.loads(ws.recv(timeout=5))
            self.assertEqual(second['start_offset'], 100)
            self.assertEqual(second['end_offset'], 200)
            ws.send(json.dumps({'type': 'stop'}))
            stopped = json.loads(ws.recv(timeout=5))
            self.assertEqual(stopped, {'type': 'stopped', 'finals': 2})
        self.assertTrue(self.server.wait_for_finals('s-1', 2, timeout=1))
        self.assertEqual(len(self.server.finals_for('s-1')), 2)

    def test_unknown_source_emits_nothing(self):
        with self.client() as ws:
            self.assertEqual(self.start(ws, source_id='other:mic')['type'], 'started')
            ws.send(b'\x00\x00' * 10)
            with self.assertRaises(TimeoutError):
                ws.recv(timeout=0.2)

    def test_bad_protocol(self):
        with self.client() as ws:
            message = self.start(ws, protocol=1)
            self.assertEqual(message['type'], 'error')
            self.assertEqual(message['code'], 'bad_protocol')

    def test_first_message_must_be_start(self):
        with self.client() as ws:
            ws.send(json.dumps({'type': 'stop'}))
            message = json.loads(ws.recv(timeout=5))
            self.assertEqual(message['code'], 'bad_message')


if __name__ == '__main__':
    unittest.main()
