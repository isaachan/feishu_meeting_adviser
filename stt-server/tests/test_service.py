import asyncio
import json
import struct
import threading
import time
import unittest
from pathlib import Path

from stt_server.service import SttService


class FakeSherpaRecognizer:
    """Scripted recognizer: text appears after the first audio batch and an
    endpoint fires after every 3rd batch."""

    def __init__(self):
        self._stream = FakeStream()
        self.batches = 0
        self.text = ''
        self.endpoint = False

    def create_stream(self):
        return self._stream

    def is_ready(self, stream):
        return False

    def decode_stream(self, stream):
        pass

    def get_result(self, stream):
        if 1600 <= stream.accepted <= 3200:
            return '你好'
        return self.text

    def is_endpoint(self, stream):
        return self.endpoint

    def reset(self, stream):
        self.text = ''
        self.endpoint = False


class FakeStream:
    def __init__(self):
        self.accepted = 0

    def accept_waveform(self, rate, samples):
        self.accepted += len(samples)

    def input_finished(self):
        pass


def audio(samples: int) -> bytes:
    return struct.pack('<h', 4000) * samples


class ServiceRoundTripTests(unittest.TestCase):
    """All tests share one event loop, thread and server (like deployment)."""

    recognizers: list = []

    @classmethod
    def setUpClass(cls):
        def factory():
            recognizer = FakeSherpaRecognizer()
            cls.recognizers.append(recognizer)
            return recognizer

        cls.service = SttService(model_dir=Path('.'),
                                 recognizer_factory=factory,
                                 max_sessions=2, idle_timeout=10.0)
        cls.loop = asyncio.new_event_loop()
        cls._ready = threading.Event()
        cls._port = 0
        cls.thread = threading.Thread(target=cls._serve, daemon=True)
        cls.thread.start()
        cls._ready.wait(timeout=10)
        if not cls._port:
            raise RuntimeError('shared server did not start')

    @classmethod
    def _serve(cls):
        asyncio.set_event_loop(cls.loop)

        async def main():
            import websockets

            async with websockets.serve(cls.service.handle, '127.0.0.1', 0) as ws:
                cls._port = ws.sockets[0].getsockname()[1]
                cls._ready.set()
                await asyncio.Event().wait()

        cls.loop.create_task(main())
        cls.loop.run_forever()

    @classmethod
    def tearDownClass(cls):
        async def shutdown():
            current = asyncio.current_task()
            for task in [t for t in asyncio.all_tasks() if t is not current]:
                task.cancel()

        future = asyncio.run_coroutine_threadsafe(shutdown(), cls.loop)
        try:
            future.result(timeout=5)
        except Exception:  # noqa: BLE001 - best effort teardown
            pass
        cls.loop.call_soon_threadsafe(cls.loop.stop)
        cls.thread.join(timeout=5)
        cls.loop.close()

    def setUp(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and self.service.state.active:
            time.sleep(0.02)
        if self.service.state.active:
            self.fail('shared server has lingering sessions from a prior test')

    def connect(self):
        from websockets.sync.client import connect

        # migrated robustness: single retry on handshake timeout (flaky CI hosts)
        last_error = None
        for _ in range(3):
            try:
                return connect(f'ws://127.0.0.1:{self._port}', open_timeout=5,
                               proxy=None)
            except TimeoutError as exc:
                last_error = exc
                time.sleep(0.2)
        raise last_error

    def recv_json(self, ws, timeout=5.0):
        return json.loads(ws.recv(timeout=timeout))

    def test_full_round_trip(self):
        with self.connect() as ws:
            ws.send(json.dumps({'type': 'start', 'protocol': 1,
                                'source_id': 'mic', 'rate': 16000,
                                'channels': 1}))
            started = self.recv_json(ws)
            self.assertEqual(started['type'], 'started')
            self.assertEqual(started['protocol'], 1)

            ws.send(audio(1600))
            event = self.recv_json(ws)
            self.assertEqual(event, {'type': 'partial', 'text': '你好',
                                     'start_offset': 0, 'end_offset': 1600})
            # unchanged text must not emit another event; jump to the final
            recognizer = self.recognizers[-1]
            recognizer.text = '你好会议'
            recognizer.endpoint = True
            ws.send(audio(3200))
            event = self.recv_json(ws)
            self.assertEqual(event, {'type': 'final', 'text': '你好会议',
                                     'start_offset': 0, 'end_offset': 4800})

            # pending text at stop must be flushed and DELIVERED before stopped
            recognizer.text = '收尾句'
            ws.send(json.dumps({'type': 'stop'}))
            event = self.recv_json(ws)
            self.assertEqual(event, {'type': 'final', 'text': '收尾句',
                                     'start_offset': 4800, 'end_offset': 4800})
            stopped = self.recv_json(ws)
            self.assertEqual(stopped, {'type': 'stopped', 'finals': 2})

    def test_rejects_binary_first_frame(self):
        with self.connect() as ws:
            ws.send(audio(1600))
            error = self.recv_json(ws)
            self.assertEqual(error['type'], 'error')
            self.assertEqual(error['code'], 'bad_message')

    def test_rejects_bad_protocol_and_format(self):
        with self.connect() as ws:
            ws.send(json.dumps({'type': 'start', 'protocol': 99,
                                'rate': 16000, 'channels': 1}))
            self.assertEqual(self.recv_json(ws)['code'], 'bad_protocol')
        with self.connect() as ws:
            ws.send(json.dumps({'type': 'start', 'protocol': 1,
                                'rate': 48000, 'channels': 1}))
            self.assertEqual(self.recv_json(ws)['code'], 'unsupported_format')

    def test_rejects_odd_binary_and_unknown_control(self):
        with self.connect() as ws:
            ws.send(json.dumps({'type': 'start', 'protocol': 1,
                                'rate': 16000, 'channels': 1}))
            self.recv_json(ws)
            ws.send(b'\x01')
            self.assertEqual(self.recv_json(ws)['code'], 'bad_message')
        with self.connect() as ws:
            ws.send(json.dumps({'type': 'start', 'protocol': 1,
                                'rate': 16000, 'channels': 1}))
            self.recv_json(ws)
            ws.send(json.dumps({'type': 'fast_forward'}))
            self.assertEqual(self.recv_json(ws)['code'], 'bad_message')

    def test_session_limit(self):
        first = self.connect()
        first.send(json.dumps({'type': 'start', 'protocol': 1,
                               'rate': 16000, 'channels': 1}))
        self.recv_json(first)
        second = self.connect()
        second.send(json.dumps({'type': 'start', 'protocol': 1,
                                'rate': 16000, 'channels': 1}))
        self.recv_json(second)
        third = self.connect()
        third.send(json.dumps({'type': 'start', 'protocol': 1,
                               'rate': 16000, 'channels': 1}))
        self.assertEqual(self.recv_json(third)['code'], 'overloaded')
        first.close()
        second.close()
        third.close()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and self.service.state.active:
            time.sleep(0.05)


if __name__ == '__main__':
    unittest.main()
