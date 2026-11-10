"""Plan-1.3 acceptance: ingest gateway over a real WS loop with injected STT stubs.

Covers: auth rejection, duplicate sequence dedup, disconnect semantics, slow
consumer isolation, resume windows, finals landing in the store.
"""
import asyncio
import json
import struct
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import websockets

from advisor.ingest import IngestServer
from advisor.store import SessionStore


def frame(sequence, samples=1600):
    return struct.pack('<I', sequence) + b'\x01\x00' * samples


class FakeSink:
    def __init__(self, on_final, *, feed_delay=0.0):
        self.on_final = on_final
        self.feed_delay = feed_delay
        self.frames = []
        self.gaps = 0
        self.closed = False

    async def feed(self, pcm):
        if self.feed_delay:
            await asyncio.sleep(self.feed_delay)
        self.frames.append(len(pcm))

    async def gap(self):
        self.gaps += 1

    async def close(self):
        self.closed = True


class FakeFactory:
    def __init__(self, *, feed_delay=0.0):
        self.feed_delay = feed_delay
        self.sinks = []

    def open(self, session_id, source, on_final):
        sink = FakeSink(on_final, feed_delay=self.feed_delay)
        self.sinks.append(sink)
        return sink


class IngestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'adviser.sqlite3'
        SessionStore.initialize(self.path)
        self.store = SessionStore(self.path)
        self.addCleanup(self.store.close)
        session = self.store.create_session(
            owner_open_id='ou_A', meeting_no='123456789', title='周会',
            event_calendar_id='cal_x', event_id='evt_y', description='材料',
            now=1_000_000.0)
        self.session_id = session['session_id']
        self.token = self.store.session_token(self.session_id)['stream_token']
        self.factory = FakeFactory()
        self.server = IngestServer(self.store, self.factory, host='127.0.0.1', port=0,
                                   idle_timeout_s=2.0, ack_every_frames=2,
                                   ack_interval_s=3600)
        self.started = threading.Event()
        self._stopping = False
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()
        self.started.wait(timeout=5)
        self.addCleanup(self._shutdown)

    def _serve(self):
        asyncio.set_event_loop(self.loop)

        async def main():
            async with websockets.serve(self.server.handler, '127.0.0.1', 0) as ws_server:
                self.port = ws_server.sockets[0].getsockname()[1]
                self.started.set()
                while not self._stopping:
                    await asyncio.sleep(0.05)
        try:
            self.loop.run_until_complete(main())
        except Exception:
            self.started.set()

    def _shutdown(self):
        self._stopping = True
        self.thread.join(timeout=5)

    def run_coro(self, coro, timeout=5):
        future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(timeout=timeout)

    def client(self):
        async def connect():
            return await websockets.connect(f'ws://127.0.0.1:{self.port}/ingest',
                                            max_size=2**22, proxy=None)
        return self.run_coro(connect())

    def recv(self, ws, timeout=5):
        async def call():
            return await asyncio.wait_for(ws.recv(), timeout)
        return self.run_coro(call(), timeout + 1)

    def send(self, ws, payload):
        async def call():
            await ws.send(payload)
        self.run_coro(call())

    def close(self, ws):
        async def call():
            await ws.close()
        self.run_coro(call())

    def wait_closed(self, ws, timeout=5):
        async def call():
            await asyncio.wait_for(ws.wait_closed(), timeout)
        self.run_coro(call(), timeout + 1)

    def start_payload(self, **overrides):
        payload = {'type': 'start', 'protocol': 1, 'session_id': self.session_id,
                   'stream_token': self.token, 'source': 'loopback',
                   'event': 'cal_x/evt_y', 'resume': False, 'first_sequence': 1,
                   'wall_clock_at': '2026-11-10T14:00:00+08:00'}
        payload.update(overrides)
        return json.dumps(payload)

    def wait_source_released(self, source='loopback'):
        deadline = time.monotonic() + 2
        while (self.session_id, source) in self.server._live and time.monotonic() < deadline:
            time.sleep(0.02)


class HappyPathTests(IngestBase):
    def test_start_frames_ack_stream_end(self):
        ws = self.client()
        self.send(ws, self.start_payload())
        started = json.loads(self.recv(ws))
        self.assertEqual(started['type'], 'started')
        self.assertEqual(started['acked_sequence'], 0)
        for seq in (1, 2):
            self.send(ws, frame(seq))
        ack = json.loads(self.recv(ws))
        self.assertEqual(ack, {'type': 'ack', 'sequence': 2})
        self.send(ws, json.dumps({'type': 'stream_end', 'last_sequence': 2}))
        stopped = json.loads(self.recv(ws))
        self.assertEqual(stopped, {'type': 'stopped', 'acked_sequence': 2})
        self.wait_closed(ws)
        sink = self.factory.sinks[0]
        self.assertEqual(len(sink.frames), 2)
        self.assertTrue(sink.closed)
        source = self.store.source_row(self.session_id, 'loopback')
        self.assertEqual(source['state'], 'drained')
        self.assertEqual(source['acked_sequence'], 2)

    def test_finals_become_captions_and_dedup(self):
        ws = self.client()
        self.send(ws, self.start_payload())
        self.recv(ws)
        sink = self.factory.sinks[0]
        sink.on_final('你好会议', 0, 48000, 'tag-1')
        sink.on_final('你好会议', 0, 48000, 'tag-1')  # duplicate final
        self.assertEqual(self.store.context_count(self.session_id), 1)
        self.send(ws, json.dumps({'type': 'stream_end', 'last_sequence': 0}))
        self.recv(ws)

    def test_gap_and_stop_forwarded(self):
        ws = self.client()
        self.send(ws, self.start_payload())
        self.recv(ws)
        self.send(ws, json.dumps({'type': 'gap'}))
        self.send(ws, frame(1))
        self.send(ws, json.dumps({'type': 'stop'}))
        self.send(ws, frame(2))
        ack = json.loads(self.recv(ws))
        self.assertEqual(ack['sequence'], 2)
        sink = self.factory.sinks[0]
        self.assertEqual(sink.gaps, 2)
        self.assertEqual(len(sink.frames), 2)


class RejectionTests(IngestBase):
    def expect_error(self, ws, code):
        message = json.loads(self.recv(ws))
        self.assertEqual(message['type'], 'error')
        self.assertEqual(message['code'], code)

    def test_bad_token(self):
        ws = self.client()
        self.send(ws, self.start_payload(stream_token='st-wrong'))
        self.expect_error(ws, 'bad_token')
        self.wait_closed(ws)

    def test_unknown_session(self):
        ws = self.client()
        self.send(ws, self.start_payload(session_id='s-nope'))
        self.expect_error(ws, 'bad_token')

    def test_session_ended(self):
        self.store.set_phase(self.session_id, 'ended', now=1_000_001)
        ws = self.client()
        self.send(ws, self.start_payload())
        self.expect_error(ws, 'session_ended')

    def test_bad_binding(self):
        ws = self.client()
        self.send(ws, self.start_payload(event='cal_x/other'))
        self.expect_error(ws, 'bad_binding')

    def test_bad_protocol(self):
        ws = self.client()
        self.send(ws, self.start_payload(protocol=9))
        self.expect_error(ws, 'bad_protocol')

    def test_source_conflict(self):
        first = self.client()
        self.send(first, self.start_payload())
        self.recv(first)
        second = self.client()
        self.send(second, self.start_payload())
        self.expect_error(second, 'source_conflict')
        self.wait_closed(second)
        # releasing on disconnect allows a new connection
        self.close(first)
        self.wait_source_released()
        third = self.client()
        self.send(third, self.start_payload(resume=True, first_sequence=1))
        message = json.loads(self.recv(third))
        self.assertEqual(message['type'], 'started')


class ResumeTests(IngestBase):
    def prime(self, upto):
        ws = self.client()
        self.send(ws, self.start_payload())
        self.recv(ws)
        for seq in range(1, upto + 1):
            self.send(ws, frame(seq))
        self.recv(ws)  # ack (every 2 frames)
        self.close(ws)
        self.wait_source_released()

    def test_resume_overlap_dedup(self):
        self.prime(4)
        ws = self.client()
        self.send(ws, self.start_payload(resume=True, first_sequence=3))
        started = json.loads(self.recv(ws))
        self.assertEqual(started['acked_sequence'], 4)
        for seq in (3, 4, 5, 6):  # 3-4 are overlap, 5-6 are new
            self.send(ws, frame(seq))
        ack = json.loads(self.recv(ws))
        self.assertEqual(ack['sequence'], 6)
        self.assertEqual(len(self.factory.sinks[1].frames), 2)  # overlap not re-fed

    def test_resume_gap_recorded(self):
        self.prime(4)
        ws = self.client()
        self.send(ws, self.start_payload(resume=True, first_sequence=20))
        json.loads(self.recv(ws))
        source = self.store.source_row(self.session_id, 'loopback')
        self.assertEqual((source['lost_from'], source['lost_to']), (5, 19))

    def test_resume_too_old_rejected(self):
        self.server.ack_every_frames = 50  # fewer durable acks; keep the test fast
        self.prime(700)
        self.assertEqual(self.store.source_row(self.session_id, 'loopback')['acked_sequence'], 700)
        ws = self.client()
        self.send(ws, self.start_payload(resume=True, first_sequence=1))
        message = json.loads(self.recv(ws))
        self.assertEqual(message['code'], 'stale_sequence')


class IsolationTests(IngestBase):
    def setUp(self):
        super().setUp()
        self.other = self.store.create_session(
            owner_open_id='ou_Z', meeting_no='987654321', title='另一场',
            event_calendar_id='cal_z', event_id='evt_z', now=1_000_000.0)
        self.other_token = self.store.session_token(self.other['session_id'])['stream_token']

    def test_slow_consumer_does_not_block_other_session(self):
        slow = self.client()
        self.send(slow, self.start_payload())
        self.recv(slow)
        fast = self.client()
        payload = {'type': 'start', 'protocol': 1, 'session_id': self.other['session_id'],
                   'stream_token': self.other_token, 'source': 'loopback',
                   'event': 'cal_z/evt_z', 'resume': False, 'first_sequence': 1,
                   'wall_clock_at': '2026-11-10T14:00:00+08:00'}
        self.send(fast, json.dumps(payload))
        self.recv(fast)
        # slow sink blocks session A's frames, but session B still acks
        self.send(fast, frame(1))
        self.send(fast, frame(2))
        ack = json.loads(self.recv(fast, timeout=2))
        self.assertEqual(ack['sequence'], 2)
        self.send(slow, frame(1))
        self.close(slow)
        self.close(fast)


class IdleTests(IngestBase):
    def test_idle_timeout_closes_with_error(self):
        ws = self.client()
        self.send(ws, self.start_payload())
        self.recv(ws)
        self.server.idle_timeout_s = 0.3
        message = json.loads(self.recv(ws, timeout=5))
        self.assertEqual((message['type'], message['code']), ('error', 'idle_timeout'))
        self.wait_closed(ws)


if __name__ == '__main__':
    unittest.main()
