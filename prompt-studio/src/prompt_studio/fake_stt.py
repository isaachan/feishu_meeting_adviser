"""Scripted STT backend speaking CONTRACT_STT_STREAM v2.

The real advisor opens one outbound STT connection per ingest source and sends
``start`` with ``source_id = f'{session_id}:{source}'``. This stub returns one
scripted ``final`` for each PCM chunk the advisor forwards, so one recorder
frame maps to one replayed line. Scripts are registered by the replay
orchestrator before the first frame; unknown sources simply produce no finals.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import sys
import threading
import time


@dataclass(frozen=True)
class Final:
    source_id: str
    text: str
    start_offset: int
    end_offset: int


class FakeSttServer:
    def __init__(self):
        self._scripts: dict[str, list[str]] = {}
        self._finals: list[Final] = []
        self._condition = threading.Condition()
        self._started = threading.Event()
        self._stop = None
        self._loop = asyncio.new_event_loop()
        self._thread: threading.Thread | None = None
        self.port = 0
        self.start_error: BaseException | None = None

    def start(self) -> 'FakeSttServer':
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()
        self._started.wait(timeout=5)
        if self.start_error is not None:
            raise RuntimeError(f'fake STT failed: {self.start_error!r}')
        return self

    def _serve(self):
        asyncio.set_event_loop(self._loop)

        async def main():
            import websockets
            async with websockets.serve(self._handle, '127.0.0.1', 0,
                                        max_size=2 ** 21) as server:
                self.port = server.sockets[0].getsockname()[1]
                self._stop = asyncio.Event()
                self._started.set()
                await self._stop.wait()
        try:
            self._loop.run_until_complete(main())
        except BaseException as exc:  # noqa: BLE001 - surface startup faults
            self.start_error = exc
            sys.stderr.write(f'fake STT failed: {exc!r}\n')
            self._started.set()
        finally:
            self._loop.close()

    def stop(self):
        if self._stop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread is not None:
            self._thread.join(timeout=5)

    @property
    def url(self) -> str:
        return f'ws://127.0.0.1:{self.port}'

    def register(self, session_id: str, source: str, texts: list[str]) -> None:
        with self._condition:
            self._scripts[f'{session_id}:{source}'] = list(texts)

    def forget(self, session_id: str) -> None:
        with self._condition:
            for key in [key for key in self._scripts
                        if key.startswith(f'{session_id}:')]:
                self._scripts.pop(key, None)
            self._finals = [item for item in self._finals
                            if not item.source_id.startswith(f'{session_id}:')]
            self._condition.notify_all()

    def finals_for(self, session_id: str) -> list[Final]:
        with self._condition:
            return [item for item in self._finals
                    if item.source_id.startswith(f'{session_id}:')]

    def wait_for_finals(self, session_id: str, count: int,
                        timeout: float = 10.0) -> bool:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                if len(self.finals_for(session_id)) >= count:
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)

    async def _handle(self, ws):
        from websockets.exceptions import ConnectionClosed
        try:
            await self._serve_connection(ws)
        except ConnectionClosed:
            pass

    async def _serve_connection(self, ws):
        try:
            start = json.loads(await ws.recv())
        except (ValueError, UnicodeDecodeError):
            await self._error(ws, 'bad_message', 'start must be JSON')
            return
        if not isinstance(start, dict) or start.get('type') != 'start':
            await self._error(ws, 'bad_message', 'first message must be start')
            return
        source_id = start.get('source_id')
        if not isinstance(source_id, str) or not source_id:
            await self._error(ws, 'bad_message', 'source_id required')
            return
        if start.get('protocol') != 2:
            await self._error(ws, 'bad_protocol', 'unsupported protocol')
            return
        with self._condition:
            lines = list(self._scripts.get(source_id, ()))
        await ws.send(json.dumps({'type': 'started', 'protocol': 2,
                                  'model': 'prompt-studio-fake',
                                  'source_id': source_id}))
        offset = 0
        cursor = 0
        async for raw in ws:
            if isinstance(raw, bytes):
                samples = len(raw) // 2
                if cursor < len(lines):
                    text = lines[cursor]
                    cursor += 1
                    end = offset + samples
                    await ws.send(json.dumps({
                        'type': 'final', 'text': text,
                        'start_offset': offset, 'end_offset': end}))
                    with self._condition:
                        self._finals.append(Final(source_id, text, offset, end))
                        self._condition.notify_all()
                    offset = end
                else:
                    offset += samples
                continue
            try:
                message = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                await self._error(ws, 'bad_message', 'invalid JSON')
                return
            kind = message.get('type') if isinstance(message, dict) else None
            if kind == 'gap':
                continue
            if kind == 'stop':
                await ws.send(json.dumps({'type': 'stopped', 'finals': cursor}))
                await ws.close()
                return
            await self._error(ws, 'bad_message', f'unknown type {kind}')
            return

    @staticmethod
    async def _error(ws, code: str, message: str):
        await ws.send(json.dumps({'type': 'error', 'code': code,
                                  'message': message}))
        await ws.close()
