"""Plan-2.2 acceptance: uplink client against a scripted fake ingest server.

Covers reconnect+retransmit dedup, token expiry exit code, stale-sequence
epoch reset, clean stream_end, Ctrl-C (no stream_end)."""
import asyncio
import json
import struct
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import websockets

from recorder.audio import CaptureSession, FakeSource
from recorder.audio.frames import AudioFrame
from recorder.uplink import SourceUplink, float32_to_int16

OPENER_THREAD_LOOP = None


class FakeIngest:
    """Scripted server: records frames per source, honors ack cadence, and can
    be told to drop connections or reject tokens."""

    def __init__(self):
        self.frames: dict[str, list[int]] = {}
        self.gaps: dict[str, int] = {}
        self.stream_ends: dict[str, int] = {}
        self.acked: dict[str, int] = {}
        self.bad_tokens = set()
        self.drop_after: dict[str, int] = {}       # source -> close after N frames
        self.stale_for: set[str] = set()            # reply stale_sequence once
        self.loop = asyncio.new_event_loop()
        self.started = threading.Event()
        self.sessions = 0

    def start(self):
        threading.Thread(target=self._serve, daemon=True).start()
        self.started.wait(timeout=5)
        return self

    def _serve(self):
        asyncio.set_event_loop(self.loop)

        async def handler(ws):
            self.sessions += 1
            start = json.loads(await ws.recv())
            source = start.get('source')
            if start.get('stream_token') in self.bad_tokens:
                await ws.send(json.dumps({'type': 'error', 'code': 'bad_token'}))
                await ws.close()
                return
            if source in self.stale_for:
                self.stale_for.discard(source)
                await ws.send(json.dumps({'type': 'error', 'code': 'stale_sequence'}))
                await ws.close()
                return
            self.frames.setdefault(source, [])
            self.gaps.setdefault(source, 0)
            acked = self.acked.get(source, 0)
            await ws.send(json.dumps({'type': 'started', 'stream_id': f'x-{source}',
                                      'acked_sequence': acked}))
            count = 0
            try:
                async for raw in ws:
                    if isinstance(raw, bytes):
                        (seq,) = struct.unpack('<I', raw[:4])
                        if seq > acked:
                            acked = seq
                            self.acked[source] = acked
                            self.frames[source].append(seq)
                        count += 1
                        await ws.send(json.dumps({'type': 'ack', 'sequence': acked}))
                        limit = self.drop_after.get(source)
                        if limit is not None and count >= limit:
                            await ws.close()
                            return
                    else:
                        message = json.loads(raw)
                        if message.get('type') == 'gap':
                            self.gaps[source] += 1
                        elif message.get('type') == 'stream_end':
                            self.stream_ends[source] = message['last_sequence']
                            await ws.send(json.dumps({'type': 'stopped',
                                                      'acked_sequence': acked}))
                            await ws.close()
                            return
            except websockets.ConnectionClosed:
                pass

        async def main():
            async with websockets.serve(handler, '127.0.0.1', 0,
                                        max_size=2 ** 21) as server:
                self.port = server.sockets[0].getsockname()[1]
                self.started.set()
                try:
                    while True:
                        await asyncio.sleep(0.05)
                finally:
                    server.close()
                    await server.wait_closed()
        try:
            self.loop.run_until_complete(main())
        except Exception:
            self.started.set()

    def stop(self):
        self.loop.call_soon_threadsafe(self.loop.stop)

    @property
    def url(self):
        return f'ws://127.0.0.1:{self.port}/ingest'


class UplinkBase(unittest.TestCase):
    def setUp(self):
        self.server = FakeIngest().start()
        self.addCleanup(self.server.stop)

    def make_uplink(self, source='loopback', **overrides):
        params = dict(session_id='s-test1', event='cal_x/evt_y', url=self.server.url,
                      token='st-goodtoken0123456789')
        params.update(overrides)
        uplink = SourceUplink(source, **params)
        uplink.start()
        self.addCleanup(uplink.join)
        return uplink

    def wait_for(self, predicate, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def frame(self, seq, count=100):
        import array
        return AudioFrame('loopback', seq, 0, 16000, 1,
                          array.array('f', [0.1] * count).tobytes(),
                          time.monotonic())


class HappyPathTests(UplinkBase):
    def test_frames_acked_and_clean_stream_end(self):
        uplink = self.make_uplink()
        for seq in (1, 2, 3):
            uplink.offer(self.frame(seq))
        self.assertTrue(self.wait_for(lambda: self.server.acked.get('loopback') == 3))
        self.assertEqual(uplink.sent_frames, 3)
        uplink.stop_clean()
        self.wait_for(lambda: uplink.stopped.is_set())
        self.assertEqual(uplink.exit_code, 0)
        self.assertEqual(self.server.stream_ends['loopback'], 3)
        self.assertEqual(len(uplink.buffer), 0)

    def test_gap_event_forwarded(self):
        from recorder.audio.frames import SourceEvent
        uplink = self.make_uplink()
        uplink.offer(SourceEvent('loopback', 'gap_begin', time.monotonic()))
        uplink.offer(self.frame(1))
        self.assertTrue(self.wait_for(lambda: self.server.gaps.get('loopback', 0) >= 1))

    def test_ctrl_c_leaves_no_stream_end(self):
        uplink = self.make_uplink()
        uplink.offer(self.frame(1))
        self.wait_for(lambda: self.server.acked.get('loopback') == 1)
        uplink.stop_interrupt()
        uplink.join(timeout=3)
        self.assertEqual(self.server.stream_ends, {})
        self.assertEqual(uplink.exit_code, 0)


class ReconnectTests(UplinkBase):
    def test_reconnect_retransmits_only_unacked(self):
        self.server.drop_after['loopback'] = 1  # drop after first frame each time
        uplink = self.make_uplink()
        for seq in (1, 2, 3, 4):
            uplink.offer(self.frame(seq))
        self.assertTrue(self.wait_for(lambda: len(self.server.frames.get('loopback', [])) >= 2),
                        f'frames={self.server.frames}')
        self.server.drop_after.pop('loopback')
        self.assertTrue(self.wait_for(lambda: self.server.acked.get('loopback') == 4),
                        f'acked={self.server.acked} sent={uplink.sent_frames}')
        self.assertGreaterEqual(uplink.reconnects, 1)
        self.assertEqual(uplink.exit_code, 0)

    def test_bad_token_exits_with_code_2(self):
        self.server.bad_tokens.add('st-wrong0000000000000')
        uplink = self.make_uplink(token='st-wrong0000000000000')
        uplink.offer(self.frame(1))
        self.wait_for(lambda: uplink.stopped.is_set())
        self.assertEqual(uplink.exit_code, 2)
        self.assertEqual(uplink.exit_reason.split(':')[0], 'bad_token')

    def test_stale_sequence_resets_epoch_and_continues(self):
        self.server.stale_for.add('loopback')
        uplink = self.make_uplink()
        self.wait_for(lambda: uplink.reconnects >= 1 or self.server.sessions >= 2)
        uplink.offer(self.frame(50))
        self.assertTrue(self.wait_for(lambda: self.server.acked.get('loopback') == 50))

    def test_retries_exhausted_exits_with_code_3(self):
        uplink = self.make_uplink(url='ws://127.0.0.1:1/ingest', reconnect_limit=2)
        uplink.offer(self.frame(1))
        self.wait_for(lambda: uplink.stopped.is_set(), timeout=15)
        self.assertEqual(uplink.exit_code, 3)


class ConversionTests(unittest.TestCase):
    def test_float32_to_int16_clamps(self):
        import array
        pcm = float32_to_int16(array.array('f', [0.0, 1.0, -1.0, 2.0]).tobytes())
        values = array.array('h')
        values.frombytes(pcm)
        self.assertEqual(list(values), [0, 32766, -32766, 32766])


class CaptureIntegrationTests(UplinkBase):
    def test_capture_session_feeds_uplink(self):
        import queue as queue_module
        with TemporaryDirectory() as tmp:
            source = FakeSource('loopback')
            session = CaptureSession([source], Path(tmp) / 'retention')
            subscriber = session.subscribe()
            session.start()
            uplink = self.make_uplink()
            done = threading.Event()

            def pump():
                while not done.is_set():
                    try:
                        uplink.offer(subscriber.get(timeout=0.1))
                    except queue_module.Empty:
                        continue

            pumper = threading.Thread(target=pump, daemon=True)
            pumper.start()
            try:
                source.push_frames(3)
                source.push_frames(2)
                self.assertTrue(self.wait_for(lambda: uplink.sent_frames >= 2),
                                f'sent={uplink.sent_frames}')
            finally:
                done.set()
                pumper.join(timeout=2)
                session.stop()


if __name__ == '__main__':
    unittest.main()
