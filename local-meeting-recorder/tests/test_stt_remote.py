import asyncio
import json
import struct
import threading
import time
import unittest
from pathlib import Path

from local_meeting_recorder.audio import AudioFrame
from local_meeting_recorder.stt import (
    FINAL,
    PARTIAL,
    RemoteStreamSession,
    SttEvent,
)

RATE = 16000


def float32(samples: int, value: float = 0.25) -> bytes:
    return struct.pack('<f', value) * samples


def frame(offset: int, samples: int) -> AudioFrame:
    return AudioFrame('mic', 1, offset, RATE, 1, float32(samples), 0.0)


class FakeConnection:
    """Scripted transport for RemoteStreamSession (no real websocket)."""

    def __init__(self, script):
        self.script = list(script)  # items: ('text', obj) | ('bytes', b'..')
        self.sent: list = []
        self.closed = threading.Event()

    def send(self, payload):
        if isinstance(payload, str):
            self.sent.append(('text', json.loads(payload)))
        else:
            self.sent.append(('audio', payload))

    def recv(self, timeout=None):
        if self.script:
            kind, value = self.script.pop(0)
            return value if kind == 'text' else value
        # idle: block until closed, like a real quiet connection
        if self.closed.wait(timeout if timeout is not None else 60.0):
            raise RuntimeError('connection closed')
        raise TimeoutError

    def close(self):
        self.closed.set()


def started_reply():
    return ('text', json.dumps({'type': 'started', 'protocol': 1,
                                'model': 'fake'}))


class RemoteStreamSessionTests(unittest.TestCase):
    def make(self, script):
        conn = FakeConnection([started_reply()] + script)
        session = RemoteStreamSession('mic', url='ws://ignored',
                                      connect=lambda: conn,
                                      clock=time.monotonic)
        session.start()
        return session, conn

    def test_start_handshake_and_feed_converts_to_int16(self):
        session, conn = self.make([])
        session.feed(frame(0, 1600))
        kind, payload = conn.sent[-1]
        self.assertEqual(kind, 'audio')
        self.assertEqual(len(payload), 3200)  # 1600 samples int16
        first = conn.sent[0]
        self.assertEqual(first[1]['type'], 'start')
        self.assertEqual(first[1]['protocol'], 1)

    def test_partial_and_final_events_parsed_with_offsets(self):
        session, conn = self.make([
            ('text', json.dumps({'type': 'partial', 'text': '你好',
                                 'start_offset': 0, 'end_offset': 1600})),
        ])
        events = session.feed(frame(0, 1600))  # events may ride with feed()
        deadline = time.monotonic() + 2
        while not events and time.monotonic() < deadline:
            events = session._collect(wait=False)
            if events:
                break
            time.sleep(0.05)
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0].status, events[0].text,
                          events[0].end_offset), (PARTIAL, '你好', 1600))

    def test_gap_waits_for_final(self):
        session, conn = self.make([
            ('text', json.dumps({'type': 'final', 'text': '断句',
                                 'start_offset': 0, 'end_offset': 3200})),
        ])
        deadline = time.monotonic() + 2
        events = []
        while time.monotonic() < deadline:
            events = session.feed_gap()
            if events:
                break
            time.sleep(0.1)
            break
        # events may arrive after the first gap wait; collect again
        if not events:
            events = session._collect(wait=True)
        self.assertEqual(events[0].status, FINAL)

    def test_close_sends_stop_and_drains(self):
        session, conn = self.make([
            ('text', json.dumps({'type': 'stopped', 'finals': 0})),
        ])
        events = session.close()
        self.assertEqual(conn.sent[-1], ('text', {'type': 'stop'}))
        self.assertTrue(conn.closed.is_set())
        self.assertEqual(events, [])

    def test_server_error_marks_session_dead(self):
        session, conn = self.make([
            ('text', json.dumps({'type': 'error', 'code': 'internal',
                                 'message': 'boom'})),
        ])
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not session.failed:
            time.sleep(0.05)
        self.assertTrue(session.failed)
        self.assertIn('boom', session.error)
        self.assertEqual(session.feed(frame(0, 1600)), [])  # dead: no-op
        session.close()


class ProtocolRoundTripTests(unittest.TestCase):
    """RemoteStreamSession against a minimal in-process protocol server."""

    def setUp(self):
        import websockets

        self.received: list = []
        self.scripted_events = [
            {'type': 'partial', 'text': '你好', 'start_offset': 0,
             'end_offset': 3200},
            {'type': 'final', 'text': '你好会议', 'start_offset': 0,
             'end_offset': 6400},
        ]
        ready = threading.Event()
        self.loop = asyncio.new_event_loop()

        async def handler(ws):
            start = json.loads(await ws.recv())
            await ws.send(json.dumps({'type': 'started', 'protocol': 1,
                                      'model': 'test'}))
            events = list(self.scripted_events)
            async for message in ws:
                if isinstance(message, bytes):
                    self.received.append(('audio', len(message)))
                    if events:
                        await ws.send(json.dumps(events.pop(0)))
                else:
                    control = json.loads(message)
                    self.received.append(('control', control['type']))
                    if control['type'] == 'stop':
                        await ws.send(json.dumps({'type': 'stopped',
                                                  'finals': 1}))
                        return

        async def main():
            async with websockets.serve(handler, '127.0.0.1', 0) as server:
                self.port = server.sockets[0].getsockname()[1]
                ready.set()
                await asyncio.Event().wait()

        threading.Thread(target=lambda: (asyncio.set_event_loop(self.loop),
                                         self.loop.run_until_complete(main())),
                         daemon=True).start()
        self.assertTrue(ready.wait(timeout=5))

    def tearDown(self):
        self.loop.call_soon_threadsafe(self.loop.stop)

    def test_round_trip_over_real_websocket(self):
        from local_meeting_recorder.stt import RemoteStreamSession

        session = RemoteStreamSession(
            'mic', url=f'ws://127.0.0.1:{self.port}')
        session.start()
        session.feed(frame(0, 1600))
        session.feed(frame(1600, 1600))
        deadline = time.monotonic() + 5
        collected: list[SttEvent] = []
        while time.monotonic() < deadline:
            collected += session._collect(wait=False)
            if any(e.status == FINAL for e in collected):
                break
            time.sleep(0.05)
        session.close()
        kinds = [event.status for event in collected]
        self.assertIn(PARTIAL, kinds)
        self.assertIn(FINAL, kinds)
        final = next(e for e in collected if e.status == FINAL)
        self.assertEqual((final.text, final.end_offset), ('你好会议', 6400))
        audio_sizes = [size for kind, size in self.received if kind == 'audio']
        self.assertEqual(audio_sizes, [3200, 3200])


if __name__ == '__main__':
    unittest.main()
