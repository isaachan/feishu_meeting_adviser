"""CaptureJob orchestration against a fake ingest server + FakeSource.

Requires recorder + websockets (skipped when recorder is not installed);
mirrors recorder's test_uplink fake server, trimmed to the GUI's needs.
"""
import asyncio
import json
import struct
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

try:
    import websockets
    from recorder.audio import FakeSource
    from macos_gui.capture import CaptureJob
    RECORDER_READY = True
except ImportError:
    RECORDER_READY = False

ARGS = {'session': 's-test', 'event': 'cal_x/evt_y',
        'url': '', 'token': 'st-test-token-0123456789', 'mic': False}


class FakeIngest:
    def __init__(self):
        self.frames = {}
        self.stream_ends = {}
        self.acked = {}
        self.bad_tokens = set()
        self.connections = 0
        self.loop = asyncio.new_event_loop()
        self.started = threading.Event()
        self.port = None

    def start(self):
        threading.Thread(target=self._serve, daemon=True).start()
        self.started.wait(timeout=5)
        return self

    def _serve(self):
        asyncio.set_event_loop(self.loop)

        async def handler(ws):
            self.connections += 1
            start = json.loads(await ws.recv())
            source = start.get('source')
            if start.get('stream_token') in self.bad_tokens:
                await ws.send(json.dumps({'type': 'error', 'code': 'bad_token'}))
                await ws.close()
                return
            self.frames.setdefault(source, [])
            acked = self.acked.get(source, 0)
            await ws.send(json.dumps({'type': 'started', 'stream_id': f'x-{source}',
                                      'acked_sequence': acked}))
            try:
                async for raw in ws:
                    if isinstance(raw, bytes):
                        (seq,) = struct.unpack('<I', raw[:4])
                        if seq > acked:
                            acked = seq
                            self.acked[source] = acked
                            self.frames[source].append(seq)
                        await ws.send(json.dumps({'type': 'ack', 'sequence': acked}))
                    else:
                        message = json.loads(raw)
                        if message.get('type') == 'stream_end':
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
                await asyncio.Future()

        self.loop.run_until_complete(main())


def wait_until(predicate, timeout=10.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


@unittest.skipUnless(RECORDER_READY, 'recorder/websockets not installed')
class CaptureJobTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeIngest().start()
        self.addCleanup(self.server.loop.call_soon_threadsafe,
                        self.server.loop.stop)
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out_root = Path(self.tmp.name) / 'audio'

    def make_job(self, **overrides):
        args = dict(ARGS)
        args['url'] = f'ws://127.0.0.1:{self.server.port}/ingest'
        args.update(overrides)
        source = FakeSource('loopback')
        job = CaptureJob(args, out_root=self.out_root, sources=[source])
        return job, source

    def test_clean_stop_sends_stream_end_and_writes_wav(self):
        job, source = self.make_job()
        job.start()
        self.assertTrue(wait_until(lambda: self.server.connections >= 1),
                        'uplink never connected')
        for _ in range(30):
            source.push_frames(1)
        self.assertTrue(
            wait_until(lambda: job.stats()['sources']['loopback']['acked_sequence'] >= 10),
            'frames were not acked')
        job.request_stop()
        self.assertTrue(job.wait(timeout=15))
        self.assertEqual(job.result['exit_code'], 0)
        self.assertEqual(self.server.stream_ends.get('loopback'), 30)
        self.assertTrue((self.out_root / 'loopback.wav').exists())
        self.assertEqual(job.state, 'stopped')

    def test_interrupt_stop_has_no_stream_end(self):
        job, source = self.make_job()
        job.start()
        self.assertTrue(wait_until(lambda: self.server.connections >= 1))
        source.push_frames(3)
        job.request_stop(interrupt=True)
        self.assertTrue(job.wait(timeout=15))
        self.assertEqual(job.result['exit_code'], 130)
        self.assertNotIn('loopback', self.server.stream_ends)

    def test_bad_token_finishes_with_exit_2(self):
        self.server.bad_tokens.add(ARGS['token'])
        job, source = self.make_job()
        job.start()
        self.assertTrue(wait_until(lambda: self.server.connections >= 1))
        source.push_frames(2)
        self.assertTrue(job.wait(timeout=15))
        self.assertEqual(job.result['exit_code'], 2)
        self.assertIn('bad_token', job.result['sources']['loopback']['exit_reason'])

    def test_missing_args_rejected(self):
        with self.assertRaises(ValueError):
            CaptureJob({'session': 's', 'url': 'ws://x/ingest'},
                       out_root=self.out_root, sources=[FakeSource('loopback')])


if __name__ == '__main__':
    unittest.main()
