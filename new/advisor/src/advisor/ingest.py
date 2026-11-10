"""Ingest gateway: recorder WS per CONTRACT_RECORDER_STREAM v1.

One WS connection = one audio source (loopback/mic). Validation order per
contract §5: protocol -> stream token/session phase -> event binding -> source
conflict -> sequence window. Frames are fed to an injectable STT session and
the durable ack cursor (sources.acked_sequence) advances only after the frame
reached the STT pipeline; captions land in the session store (the persisted
hand-off to the supervisor loop). Slow consumers cannot block other sessions:
each connection owns its handler task and sink.
"""
import asyncio
import json
import struct
import threading
import time
from typing import Callable, Protocol

import websockets

from .errors import StorageError
from .models import Caption
from .store import SessionStore

MAX_FRAME_BYTES = 1_048_576
SOURCES = ('loopback', 'mic')


class IngestError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


class SttSession(Protocol):
    async def feed(self, pcm: bytes) -> None: ...
    async def gap(self) -> None: ...
    async def close(self) -> None: ...


class SttSessionFactory(Protocol):
    """open() returns a per-connection STT session; on_final(text, start_offset,
    end_offset, tag) is invoked for each final utterance."""

    def open(self, session_id: str, source: str,
             on_final: Callable[[str, int, int, str], None]) -> SttSession:
        raise NotImplementedError


class IngestServer:
    def __init__(self, store: SessionStore, sink_factory: SttSessionFactory, *,
                 host='0.0.0.0', port=8400, idle_timeout_s=120.0,
                 ack_every_frames=10, ack_interval_s=1.0, window_frames=600,
                 clock=time.time):
        self.store = store
        self.sink_factory = sink_factory
        self.host, self.port = host, port
        self.idle_timeout_s = idle_timeout_s
        self.ack_every_frames = ack_every_frames
        self.ack_interval_s = ack_interval_s
        self.window_frames = window_frames
        self.clock = clock
        self._live: set[tuple[str, str]] = set()
        self._live_guard = threading.Lock()
        self.dropped_frames = 0  # explicit slow-consumer accounting
        self._activity: dict[str, float] = {}
        self._connections: set = set()
        self.loop = None

    def _claim_source(self, session_id: str, source: str) -> bool:
        with self._live_guard:
            key = (session_id, source)
            if key in self._live:
                return False
            self._live.add(key)
            return True

    def _release_source(self, session_id: str, source: str) -> None:
        with self._live_guard:
            self._live.discard((session_id, source))

    def _touch(self, session_id: str) -> None:
        with self._live_guard:
            self._activity[session_id] = time.monotonic()

    def activity_snapshot(self) -> dict:
        """session_id -> monotonic last-activity (for the supervisor idle rule)."""
        with self._live_guard:
            return dict(self._activity)

    def source_connected(self, session_id: str, source: str) -> bool:
        with self._live_guard:
            return (session_id, source) in self._live

    def close_session(self, session_id: str) -> None:
        """Close all live connections of a session (draining sweep)."""
        loop = self.loop
        if loop is None:
            return
        targets = [connection.ws for connection in list(self._connections)
                   if connection.session_id == session_id]
        for websocket in targets:
            future = asyncio.run_coroutine_threadsafe(
                websocket.close(1000, 'session_draining'), loop)
            try:
                future.result(timeout=2)
            except Exception:
                pass

    async def handler(self, websocket):
        if self.loop is None:
            self.loop = asyncio.get_running_loop()
        connection = _Connection(self, websocket)
        with self._live_guard:
            self._connections.add(connection)
        try:
            await connection.run()
        finally:
            with self._live_guard:
                self._connections.discard(connection)
            if connection.claimed:
                self._release_source(connection.session_id, connection.source)


class _Connection:
    def __init__(self, server: IngestServer, websocket):
        self.server = server
        self.ws = websocket
        self.store = server.store
        self.session_id = ''
        self.source = ''
        self.speaker = '本机'
        self.claimed = False
        self.sink: SttSession | None = None
        self.acked = 0
        self.last_seq = 0
        self.since_ack = 0
        self.last_ack_at = 0.0
        self.last_activity = time.monotonic()
        self.tag = ''

    async def send(self, payload: dict):
        await self.ws.send(json.dumps(payload, ensure_ascii=False))

    async def fail(self, code, message):
        try:
            await self.send({'type': 'error', 'code': code, 'message': message})
        finally:
            await self.ws.close()

    async def run(self):
        start_raw = await self.ws.recv()
        start = self._parse_json(start_raw)
        if not isinstance(start, dict) or start.get('type') != 'start':
            await self.fail('bad_message', 'first message must be start')
            return
        if start.get('protocol') != 1:
            await self.fail('bad_protocol', 'unsupported protocol')
            return
        try:
            await self._start(start)
        except IngestError as exc:
            await self.fail(exc.code, exc.message)
            return
        await self._stream()

    def _parse_json(self, raw):
        try:
            return json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            return None

    async def _start(self, start: dict):
        session_id = start.get('session_id')
        token = start.get('stream_token')
        source = start.get('source')
        if (not isinstance(session_id, str) or not isinstance(token, str)
                or source not in SOURCES):
            raise IngestError('bad_message', 'invalid start fields')
        self.session_id = session_id
        self.source = source
        try:
            session = self.store.require_session(self.session_id)
        except StorageError:
            raise IngestError('bad_token', 'unknown session') from None
        if session['phase'] != 'recording':
            raise IngestError('session_ended', f"session is {session['phase']}")
        token_row = self.store.session_token(self.session_id)
        if token_row is None or token_row['stream_token'] != token:
            raise IngestError('bad_token', 'unknown or expired stream token')
        binding = f"{session['event_calendar_id']}/{session['event_id']}"
        if start.get('event') != binding:
            raise IngestError('bad_binding', 'event binding mismatch')
        if not self.server._claim_source(self.session_id, self.source):
            raise IngestError('source_conflict', 'source already connected')
        self.claimed = True

        first_sequence = start.get('first_sequence')
        if not isinstance(first_sequence, int) or first_sequence < 1:
            self._release_and_raise('bad_message', 'invalid first_sequence')
        self.speaker = start.get('speaker') or '本机'
        self.store.ensure_source(self.session_id, self.source)
        row = self.store.source_row(self.session_id, self.source)
        assert row is not None
        self.acked = row['acked_sequence']
        if not start.get('resume'):
            if self.acked and first_sequence != 1:
                self._release_and_raise('bad_message', 'fresh start must begin at 1')
            if self.acked:
                # New capture epoch: previous unacked range is explicitly lost.
                self.store.mark_source_lost(self.session_id, self.source,
                                            lost_from=None, lost_to=None)
                self.store.source_reset(self.session_id, self.source,
                                        first_sequence - 1)
                self.acked = first_sequence - 1
        else:
            if first_sequence < self.acked + 1 - self.server.window_frames:
                self._release_and_raise('stale_sequence', 'resume outside retention window')
            if first_sequence > self.acked + 1:
                self.store.record_source_gap(self.session_id, self.source,
                                             self.acked + 1, first_sequence - 1)
        self.last_seq = first_sequence - 1
        stream_id = f"{self.session_id}:{self.source}:{_short_uuid()}"
        self.tag = _short_uuid()
        self.sink = self.server.sink_factory.open(
            self.session_id, self.source, self._on_final)
        await self.send({'type': 'started', 'stream_id': stream_id,
                         'acked_sequence': self.acked})
        self.since_ack = 0
        self.last_ack_at = time.monotonic()

    def _release_and_raise(self, code, message):
        self.server._release_source(self.session_id, self.source)
        self.claimed = False
        raise IngestError(code, message)

    def _on_final(self, text: str, start_offset: int, end_offset: int, tag: str):
        now = self.server.clock()
        sentence_key = f'{self.source}:{tag}:{start_offset}-{end_offset}'
        caption = Caption(
            meeting_id=self.session_id, sentence_key=sentence_key, text=text,
            received_at=_iso(now), event_time=int(now * 1000),
            start_time=int((now - (end_offset - start_offset) / 16000) * 1000),
            end_time=int(now * 1000),
            speaker_name=(self.speaker if self.source == 'mic' else None))
        try:
            self.store.ingest_caption(self.session_id, caption,
                                      delivery_id=f'{self.session_id}:{sentence_key}',
                                      now=now)
        except StorageError:
            pass  # session left recording between final and apply; drop quietly

    async def _stream(self):
        idle_task = asyncio.get_event_loop().create_task(self._idle_watch())
        try:
            async for raw in self.ws:
                self.last_activity = time.monotonic()
                self.server._touch(self.session_id)
                try:
                    if isinstance(raw, bytes):
                        await self._frame(raw)
                    else:
                        message = self._parse_json(raw)
                        if not isinstance(message, dict):
                            raise IngestError('bad_message', 'invalid json')
                        kind = message.get('type')
                        if kind == 'gap':
                            await self._sink().gap()
                        elif kind == 'stop':
                            await self._sink().gap()  # v1: flush and hold open
                        elif kind == 'stream_end':
                            await self._stream_end(message)
                            return
                        else:
                            raise IngestError('bad_message', f'unknown type {kind}')
                except IngestError as exc:
                    await self.fail(exc.code, exc.message)
                    return
        except websockets.ConnectionClosed:
            pass  # recorder vanished: source stays live for reconnect/idle path
        finally:
            idle_task.cancel()
            if self.sink is not None:
                try:
                    await self.sink.close()
                except Exception:
                    pass

    def _sink(self) -> SttSession:
        if self.sink is None:
            raise IngestError('internal', 'sink missing')
        return self.sink

    async def _frame(self, raw: bytes):
        if len(raw) < 4 or len(raw) > MAX_FRAME_BYTES or (len(raw) - 4) % 2:
            raise IngestError('bad_message', 'invalid frame')
        (sequence,) = struct.unpack('<I', raw[:4])
        pcm = raw[4:]
        if sequence <= self.acked or sequence <= self.last_seq:
            self.server.dropped_frames += 1  # idempotent drop of overlap
            return
        self.last_seq = sequence
        await self._sink().feed(pcm)
        self.acked = sequence
        self.since_ack += 1
        now = time.monotonic()
        if (self.since_ack >= self.server.ack_every_frames
                or now - self.last_ack_at >= self.server.ack_interval_s):
            self.store.source_ack(self.session_id, self.source, sequence)
            await self.send({'type': 'ack', 'sequence': sequence})
            self.since_ack = 0
            self.last_ack_at = now

    async def _stream_end(self, message: dict):
        last = message.get('last_sequence')
        if not isinstance(last, int) or last < self.acked:
            raise IngestError('bad_message', 'invalid last_sequence')
        self.store.source_ack(self.session_id, self.source, last)
        self.store.mark_source_drained(self.session_id, self.source, last)
        await self.send({'type': 'stopped', 'acked_sequence': last})

    async def _idle_watch(self):
        while True:
            await asyncio.sleep(0.5)
            if time.monotonic() - self.last_activity > self.server.idle_timeout_s:
                await self.fail('idle_timeout', 'no messages within idle window')
                return


def _short_uuid():
    from uuid import uuid4
    return uuid4().hex[:8]


def _iso(now: float) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(now, datetime.timezone.utc).isoformat()


def serve_async(server: IngestServer):
    return websockets.serve(server.handler, server.host, server.port,
                            max_size=MAX_FRAME_BYTES + 64, ping_interval=20)


def start_ingest_thread(server: IngestServer):
    """Run the ingest server on a dedicated event-loop thread; returns a stop()."""
    started = threading.Event()
    loop_holder = {}

    def runner():
        loop = asyncio.new_event_loop()
        loop_holder['loop'] = loop
        asyncio.set_event_loop(loop)
        async def main():
            async with serve_async(server):
                started.set()
                await asyncio.Event().wait()  # run until stopped
        try:
            loop.run_until_complete(main())
        except Exception:
            if not started.is_set():
                started.set()

    thread = threading.Thread(target=runner, name='ingest-ws', daemon=True)
    thread.start()
    started.wait(timeout=5)

    def stop():
        loop = loop_holder.get('loop')
        if loop is not None:
            loop.call_soon_threadsafe(lambda: loop.stop())

    return stop
