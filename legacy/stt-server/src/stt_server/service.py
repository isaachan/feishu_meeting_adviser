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
import threading

from .auth import AuthError, AuthValidate
from .session import SessionConfigError, ServerSession

PROTOCOL = 2
MAX_MESSAGE_BYTES = 1 << 20


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

    async def handle(self, websocket) -> None:
        session: ServerSession | None = None
        acquired = False
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

            finals = 0
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
                    await self._fail(websocket, 'idle_timeout',
                                     f'no data for {self.idle_timeout:.0f}s')
                    return
                if isinstance(message, (bytes, bytearray, memoryview)):
                    pcm = bytes(message)
                    if not pcm or len(pcm) % 2:
                        await self._fail(websocket, 'bad_message',
                                         'binary frame must be int16 frames')
                        return
                    try:
                        events = await asyncio.to_thread(
                            session.feed_samples, pcm)
                    except Exception as exc:  # noqa: BLE001 - map to internal
                        await self._fail(websocket, 'internal', str(exc))
                        return
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
                    await _send_json(websocket, {
                        'type': event.status,
                        'text': event.text,
                        'start_offset': event.start_offset,
                        'end_offset': event.end_offset,
                    })
        except Exception:  # noqa: BLE001 - connection torn down
            if session is not None:
                session.cancel()
        finally:
            if acquired:
                self.state.release()

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
        try:
            await _send_json(websocket, {'type': 'error', 'code': code,
                                         'message': message})
            await websocket.close()
        except Exception:  # noqa: BLE001 - best effort
            pass
