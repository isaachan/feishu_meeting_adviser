"""Synchronous fake recorder for the advisor ingest WS (contract v1).

One silence frame becomes one scripted STT final (see ``fake_stt``). The replay
orchestrator sends one frame per human line, then ``stream_end``.
"""
from __future__ import annotations

import json
import struct
import time

from .clients import RecorderArgs

FRAME_SAMPLES = 1600  # 100 ms of 16 kHz mono s16le
SILENCE = b'\x00\x00' * FRAME_SAMPLES


class IngestError(RuntimeError):
    pass


class IngestSession:
    def __init__(self, recorder: RecorderArgs, *, source: str = 'mic',
                 speaker: str = '本机', frame_samples: int = FRAME_SAMPLES,
                 open_timeout: float = 10.0):
        self.recorder = recorder
        self.source = source
        self.speaker = speaker
        self.frame_samples = frame_samples
        self.open_timeout = open_timeout
        self._ws = None
        self._sequence = 0
        self._ended = False

    def start(self) -> dict:
        import websockets.sync.client as sync_client
        self._ws = sync_client.connect(
            self.recorder.url, open_timeout=self.open_timeout,
            close_timeout=5, max_size=2 ** 21, proxy=None, legacy=True)
        self._ws.send(json.dumps({
            'type': 'start', 'protocol': 1,
            'session_id': self.recorder.session,
            'stream_token': self.recorder.token,
            'source': self.source, 'event': self.recorder.event,
            'first_sequence': 1, 'speaker': self.speaker}))
        greeting = self._recv(10.0)
        if greeting.get('type') != 'started':
            raise IngestError(f'ingest start rejected: {greeting}')
        return greeting

    def send_turn(self) -> int:
        if self._ws is None or self._ended:
            raise IngestError('ingest session is not live')
        self._sequence += 1
        frame = struct.pack('<I', self._sequence) + SILENCE
        if self.frame_samples != FRAME_SAMPLES:
            frame = struct.pack('<I', self._sequence) + b'\x00\x00' * self.frame_samples
        self._ws.send(frame)
        return self._sequence

    def end(self, timeout: float = 10.0) -> dict:
        if self._ws is None:
            raise IngestError('ingest session is not live')
        if self._ended:
            return {'type': 'stopped', 'acked_sequence': self._sequence}
        self._ws.send(json.dumps({'type': 'stream_end',
                                  'last_sequence': self._sequence}))
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise IngestError('timed out waiting for stopped')
            message = self._recv(remaining)
            if message.get('type') == 'stopped':
                self._ended = True
                self._ws.close()
                return message

    def abort(self):
        if self._ws is not None and not self._ended:
            self._ended = True
            try:
                self._ws.close()
            except Exception:
                pass

    def _recv(self, timeout: float) -> dict:
        assert self._ws is not None
        try:
            raw = self._ws.recv(timeout=timeout)
        except TimeoutError:
            raise IngestError('ingest response timeout') from None
        try:
            message = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise IngestError('ingest returned invalid JSON') from None
        if not isinstance(message, dict):
            raise IngestError('ingest returned a non-object')
        if message.get('type') == 'error':
            raise IngestError(f"{message.get('code')}: {message.get('message')}")
        return message
