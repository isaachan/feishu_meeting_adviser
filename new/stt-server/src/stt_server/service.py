"""WebSocket service per CONTRACT_STT_STREAM v2.

One connection = one audio source session. Audio decode runs in a worker
thread (sherpa/onnxruntime calls are blocking); events are sent as JSON
text frames immediately after each audio batch. Organizer authorization
(auth mode `feishu`) is validated before any session slot or recognizer
is created.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import struct
import threading
import time

from .auth import AuthError, AuthValidate
from .session import SessionConfigError, ServerSession

LOG = logging.getLogger(__name__)

PROTOCOL = 2
MAX_MESSAGE_BYTES = 1 << 20
PROGRESS_LOG_S = 5.0


class ServiceState:
    def __init__(self, *, max_sessions: int = 8):
        self.max_sessions = max_sessions
        self._active = 0
        self._lock = threading.Lock()

    def acquire(self) -> bool:
        with self._lock:
            if self._active >= self.max_sessions:
                return False
            self._active += 1
            return True

    def release(self) -> None:
        with self._lock:
            self._active = max(0, self._active - 1)

    @property
    def active(self) -> int:
        with self._lock:
            return self._active


def _pcm_rms(pcm: bytes) -> float:
    """RMS of an int16 LE chunk (0.0-32768 scale); 0.0 for empty input."""
    count = len(pcm) // 2
    if not count:
        return 0.0
    samples = struct.unpack(f'<{count}h', pcm[:count * 2])
    return math.sqrt(sum(s * s for s in samples) / count)


async def _send_json(websocket, payload: dict) -> None:
    await websocket.send(json.dumps(payload, ensure_ascii=False))


class SttService:
    """Connection handler factory; owns model config and session limits."""

    def __init__(self, *, model_dir, num_threads: int = 2,
                 max_sessions: int = 8, idle_timeout: float = 60.0,
                 model_name: str = 'sherpa-zh-en-punct-int8-2026-06-05',
                 recognizer_factory=None, auth: AuthValidate | None = None):
        self.model_dir = model_dir
        self.num_threads = num_threads
        self.idle_timeout = idle_timeout
        self.model_name = model_name
        self._recognizer_factory = recognizer_factory
        self.auth = auth
        self.state = ServiceState(max_sessions=max_sessions)
        self._conn_seq = 0
        self._live: dict[int, dict] = {}
        self._live_lock = threading.Lock()

    def stats(self) -> list[dict]:
        with self._live_lock:
            return [dict(info) for info in self._live.values()]

    def _track(self, info: dict) -> int:
        info.setdefault('source_id', '')
        info['fed_samples'] = 0
        info['audio_s'] = 0.0
        info['decode_ms'] = 0.0
        info['finals'] = 0
        info['partials'] = 0
        info['rms_max'] = 0.0
        info['rms_last'] = 0.0
        info['last_partial'] = ''
        with self._live_lock:
            self._conn_seq += 1
            self._live[self._conn_seq] = info
            return self._conn_seq

    def _untrack(self, key: int) -> None:
        with self._live_lock:
            self._live.pop(key, None)

    async def handle(self, websocket) -> None:
        session: ServerSession | None = None
        acquired = False
        stats_key: int | None = None
        info: dict = {}
        finals = 0
        try:
            first = await asyncio.wait_for(websocket.recv(), self.idle_timeout)
            if not isinstance(first, str):
                await self._fail(websocket, 'bad_message',
                                 'first frame must be a text start message')
                return
            try:
                start = json.loads(first)
            except ValueError:
                await self._fail(websocket, 'bad_message', 'start is not JSON')
                return
            if not isinstance(start, dict) or start.get('type') != 'start':
                await self._fail(websocket, 'bad_message', 'expected start')
                return
            protocol = start.get('protocol')
            allowed_protocols = (2,) if self.auth is not None else (1, 2)
            if protocol not in allowed_protocols:
                await self._fail(websocket, 'bad_protocol',
                                 f'protocol {protocol} unsupported')
                return
            if start.get('rate') != 16000 or start.get('channels') != 1:
                await self._fail(websocket, 'unsupported_format',
                                 'v2 supports 16 kHz mono only')
                return
            validated = None
            if self.auth is not None:
                auth_field = start.get('auth')
                if not isinstance(auth_field, dict):
                    await self._fail(websocket, 'unauthorized',
                                     'start must carry an auth object')
                    return
                try:
                    validated = await asyncio.to_thread(
                        self.auth.validate,
                        auth_field.get('user_access_token'),
                        auth_field.get('calendar_event_id'))
                except AuthError as exc:
                    await self._fail(websocket, exc.code, exc.message)
                    return
            if not self.state.acquire():
                await self._fail(websocket, 'overloaded',
                                 f'more than {self.state.max_sessions} sessions')
                return
            acquired = True
            session = ServerSession(model_dir=self.model_dir,
                                    num_threads=self.num_threads,
                                    recognizer_factory=self._recognizer_factory)
            try:
                session.start()
            except SessionConfigError as exc:
                await self._fail(websocket, 'internal', str(exc))
                return
            await _send_json(websocket, {'type': 'started', 'protocol': protocol,
                                         'model': self.model_name,
                                         **({'user_id': validated.user_id,
                                             'calendar_event_id': validated.calendar_event_id}
                                            if validated is not None else {})})

            info = {'source_id': str(start.get('source_id') or '')}
            stats_key = self._track(info)
            LOG.info('stt session started: source_id=%s peer=%s active=%s',
                     info['source_id'], getattr(websocket, 'remote_address', None),
                     self.state.active)
            last_progress = time.monotonic()
            while True:
                try:
                    message = await asyncio.wait_for(websocket.recv(),
                                                     self.idle_timeout)
                except asyncio.TimeoutError:
                    tail = await asyncio.to_thread(session.feed_gap)
                    for event in tail:
                        if event.status == 'final':
                            finals += 1
                        await self._send_event(websocket, event)
                    LOG.warning('stt idle timeout: source_id=%s audio_s=%.1f',
                                info['source_id'], session.fed / 16000.0)
                    await self._fail(websocket, 'idle_timeout',
                                     f'no data for {self.idle_timeout:.0f}s')
                    return
                if isinstance(message, (bytes, bytearray, memoryview)):
                    pcm = bytes(message)
                    if not pcm or len(pcm) % 2:
                        await self._fail(websocket, 'bad_message',
                                         'binary frame must be int16 frames')
                        return
                    rms = _pcm_rms(pcm)
                    info['rms_last'] = round(rms, 1)
                    info['rms_max'] = max(info['rms_max'], rms)
                    try:
                        events = await asyncio.to_thread(
                            session.feed_samples, pcm)
                    except Exception as exc:  # noqa: BLE001 - map to internal
                        LOG.warning('stt feed error: source_id=%s %s',
                                    info['source_id'], exc)
                        await self._fail(websocket, 'internal', str(exc))
                        return
                    if time.monotonic() - last_progress >= PROGRESS_LOG_S:
                        last_progress = time.monotonic()
                        LOG.info('stt progress: source_id=%s audio_s=%.1f '
                                 'decode_ms=%.0f finals=%s rms_last=%s',
                                 info['source_id'], session.fed / 16000.0,
                                 session.decode_ms, finals, info['rms_last'])
                else:
                    try:
                        control = json.loads(message)
                    except ValueError:
                        await self._fail(websocket, 'bad_message',
                                         'text frame is not JSON')
                        return
                    if not isinstance(control, dict):
                        await self._fail(websocket, 'bad_message',
                                         'control frame must be an object')
                        return
                    kind = control.get('type')
                    if kind == 'gap':
                        events = await asyncio.to_thread(session.feed_gap)
                    elif kind == 'stop':
                        events = await asyncio.to_thread(session.close)
                        for event in events:
                            if event.status == 'final':
                                finals += 1
                                LOG.info('stt final(stop): source_id=%s finals=%s '
                                         'text="%s"', info['source_id'], finals,
                                         event.text[:120])
                            await self._send_event(websocket, event)
                        await _send_json(websocket,
                                         {'type': 'stopped', 'finals': finals})
                        return
                    else:
                        await self._fail(websocket, 'bad_message',
                                         f'unknown control type {kind!r}')
                        return
                for event in events:
                    if event.status == 'final':
                        finals += 1
                        LOG.info('stt final: source_id=%s finals=%s offsets=%s-%s '
                                 'text="%s"', info['source_id'], finals,
                                 event.start_offset, event.end_offset,
                                 event.text[:120])
                    else:
                        info['partials'] += 1
                        info['last_partial'] = event.text[:80]
                    await _send_json(websocket, {
                        'type': event.status,
                        'text': event.text,
                        'start_offset': event.start_offset,
                        'end_offset': event.end_offset,
                    })
                info['fed_samples'] = session.fed
                info['audio_s'] = round(session.fed / 16000.0, 1)
                info['decode_ms'] = round(session.decode_ms, 0)
                info['finals'] = finals
        except Exception:  # noqa: BLE001 - connection torn down
            LOG.warning('stt connection aborted: source_id=%s',
                        info.get('source_id', ''), exc_info=True)
            if session is not None:
                session.cancel()
        finally:
            if acquired:
                self.state.release()
            if stats_key is not None:
                self._untrack(stats_key)
                if session is not None:
                    LOG.info('stt session closed: source_id=%s audio_s=%.1f '
                             'decode_ms=%.0f finals=%s rms_max=%s',
                             info['source_id'], session.fed / 16000.0,
                             session.decode_ms, finals, round(info['rms_max'], 1))

    @staticmethod
    async def _send_event(websocket, event) -> None:
        await _send_json(websocket, {
            'type': event.status,
            'text': event.text,
            'start_offset': event.start_offset,
            'end_offset': event.end_offset,
        })

    @staticmethod
    async def _fail(websocket, code: str, message: str) -> None:
        LOG.warning('stt error sent: code=%s %s', code, message)
        try:
            await _send_json(websocket, {'type': 'error', 'code': code,
                                         'message': message})
            await websocket.close()
        except Exception:  # noqa: BLE001 - best effort
            pass
