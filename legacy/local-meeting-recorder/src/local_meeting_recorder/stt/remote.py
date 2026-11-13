"""Remote streaming STT client per CONTRACT_STT_STREAM v2.

Implements the same StreamingSession interface as the local sherpa session:
feed(frame) returns partial/final events with source-timeline offsets, but
the decode runs on the remote stt-server. Audio is transported as int16
PCM (halves uplink bandwidth); offsets count samples fed since session
start, which equals the server-side offsets by contract.

v2 authorization: when an ``auth`` provider is set, every connection's
start message carries its payload (user_access_token + calendar_event_id);
an ``auth_expired`` refusal at start triggers one silent refresh + reconnect
via ``on_auth_expired`` (contract §3.5/§4). Other refusals fail the session
with the server's error code exposed as ``error_code``.
"""
from __future__ import annotations

import array
import json
import queue
import threading
import time
from typing import Callable

from ..audio.frames import AudioFrame
from .streaming import FINAL, PARTIAL, RATE, SttEvent, StreamingSession


class RemoteStreamError(RuntimeError):
    """Transport/handshake failure; the session is dead afterwards."""


def _float32_to_int16(pcm: bytes) -> bytes:
    floats = array.array('f')
    floats.frombytes(pcm)
    return array.array('h', (int(max(-1.0, min(1.0, v)) * 32766)
                              for v in floats)).tobytes()


class RemoteStreamSession(StreamingSession):
    """One source session over one WebSocket connection."""

    def __init__(self, source_id: str, *, url: str, connect_timeout: float = 10.0,
                 gap_wait: float = 1.5, stop_wait: float = 3.0,
                 clock=time.monotonic, connect=None,
                 auth: 'Callable[[], dict] | None' = None,
                 on_auth_expired: 'Callable[[], None] | None' = None):
        self.source_id = source_id
        self.url = url
        self.connect_timeout = connect_timeout
        self.gap_wait = gap_wait
        self.stop_wait = stop_wait
        self._clock = clock
        self._connect = connect
        self._auth = auth
        self._on_auth_expired = on_auth_expired
        self._conn = None
        self._ctx = None
        self._send_lock = threading.Lock()
        self._events: queue.Queue[SttEvent | None] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._dead = False
        self._stopped_reply = threading.Event()
        self._error: str | None = None
        self._error_code: str | None = None
        self.started: dict | None = None

    @property
    def failed(self) -> bool:
        return self._dead

    @property
    def error(self) -> str | None:
        return self._error

    @property
    def error_code(self) -> str | None:
        return self._error_code

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        retries = (self._auth is not None and self._on_auth_expired is not None)
        attempts = 2 if retries else 1
        for attempt in range(attempts):
            self._open()
            payload = {'type': 'start', 'protocol': 2,
                       'source_id': self.source_id,
                       'rate': RATE, 'channels': 1}
            if self._auth is not None:
                payload['auth'] = self._auth()
            self._send(payload)
            deadline = self._clock() + self.connect_timeout
            expired = False
            while True:
                message = self._recv(deadline)
                if message.get('type') == 'started':
                    self.started = message
                    break
                if message.get('type') == 'error':
                    self._error_code = message.get('code')
                    if (attempt + 1 < attempts
                            and message.get('code') == 'auth_expired'):
                        expired = True
                        break
                    self._fail(f'server rejected start: {message}')
                # ignore other early events
            if not expired:
                break
            refresh = self._on_auth_expired
            assert refresh is not None  # retries require a refresh hook
            self._close_conn()
            refresh()  # provider refreshes its stored token
        self._reader = threading.Thread(target=self._read_loop, daemon=True,
                                        name=f'{self.source_id}-stt-recv')
        self._reader.start()

    def _open(self) -> None:
        if self._connect is not None:
            self._conn = self._connect()
        else:
            from websockets.sync.client import connect

            self._ctx = connect(self.url, open_timeout=self.connect_timeout,
                                max_size=1 << 20, proxy=None)
            self._conn = self._ctx.__enter__()

    def _read_loop(self) -> None:
        conn = self._conn
        assert conn is not None
        try:
            while True:
                raw = conn.recv()
                if isinstance(raw, str):
                    message = json.loads(raw)
                    kind = message.get('type')
                    if kind in (PARTIAL, FINAL):
                        self._events.put(SttEvent(
                            kind, message.get('text', ''),
                            message.get('start_offset', 0),
                            message.get('end_offset', 0)))
                    elif kind == 'stopped':
                        self._stopped_reply.set()
                        return
                    elif kind == 'error':
                        self._error_code = message.get('code')
                        self._error = message.get('message', 'server error')
                        self._dead = True
                        self._events.put(None)
                        return
        except Exception as exc:  # noqa: BLE001 - connection torn down
            if not self._stopped_reply.is_set():
                self._error = f'connection lost: {exc}'
                self._dead = True
                self._events.put(None)
            return

    def _recv(self, deadline: float) -> dict:
        conn = self._conn
        assert conn is not None
        timeout = max(0.1, deadline - self._clock())
        raw = conn.recv(timeout=timeout)
        if not isinstance(raw, str):
            raise RemoteStreamError('expected text frame')
        return json.loads(raw)

    def _send(self, payload: dict) -> None:
        conn = self._conn
        assert conn is not None
        with self._send_lock:
            conn.send(json.dumps(payload, ensure_ascii=False))

    def _send_audio(self, pcm: bytes) -> None:
        conn = self._conn
        assert conn is not None
        with self._send_lock:
            conn.send(pcm)

    def _fail(self, message: str) -> None:
        self._dead = True
        self._error = message
        self._close_conn()
        raise RemoteStreamError(message)

    def _close_conn(self) -> None:
        conn = self._conn
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001 - best effort
                pass
        ctx, self._ctx = getattr(self, '_ctx', None), None
        if ctx is not None:
            try:
                ctx.__exit__(None, None, None)
            except Exception:  # noqa: BLE001 - best effort
                pass

    # -- StreamingSession API -------------------------------------------------

    def feed(self, frame: AudioFrame) -> list[SttEvent]:
        if self._dead or self._conn is None:
            return []
        if frame.channels != 1 or frame.rate != RATE:
            raise ValueError('remote STT expects 16 kHz mono frames')
        try:
            self._send_audio(_float32_to_int16(frame.data))
        except Exception as exc:  # noqa: BLE001 - transport died
            self._dead = True
            self._error = f'send failed: {exc}'
            return []
        return self._collect(wait=False)

    def feed_gap(self) -> list[SttEvent]:
        if self._dead or self._conn is None:
            return []
        try:
            self._send({'type': 'gap'})
        except Exception as exc:  # noqa: BLE001
            self._dead = True
            self._error = f'send failed: {exc}'
            return []
        return self._collect(wait=True)

    def close(self) -> list[SttEvent]:
        if self._conn is None:
            return []
        if not self._dead:
            try:
                self._send({'type': 'stop'})
            except Exception as exc:  # noqa: BLE001
                self._error = f'send failed: {exc}'
        self._stopped_reply.wait(self.stop_wait)
        self._close_conn()
        if self._reader is not None:
            self._reader.join(timeout=self.stop_wait)
        return self._collect(wait=False)

    # -- helpers ---------------------------------------------------------------

    def _collect(self, *, wait: bool) -> list[SttEvent]:
        events: list[SttEvent] = []
        deadline = self._clock() + (self.gap_wait if wait else 0.0)
        while True:
            if wait:
                timeout = max(0.05, deadline - self._clock())
                try:
                    item = self._events.get(timeout=timeout)
                except queue.Empty:
                    return events
            else:
                try:
                    item = self._events.get_nowait()
                except queue.Empty:
                    return events
            if item is None:
                return events
            events.append(item)
            if wait and item.status == FINAL:
                return events  # the awaited boundary arrived
