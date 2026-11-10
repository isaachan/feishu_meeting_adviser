"""Cloud-internal STT client: one stt-server session per ingest connection.

Speaks the legacy CONTRACT_STT_STREAM v2 shape against a stt-server running
auth=none (intranet baseline per DEPLOYMENT §2). Finals are delivered through
the on_final callback supplied by the ingest gateway.
"""
import asyncio
import json

import websockets


class RemoteSttSession:
    def __init__(self, url: str, session_id: str, source: str,
                 on_final, *, connect_timeout=5.0, stop_timeout=10.0):
        self.url = url.rstrip('/')
        self.source_id = f'{session_id}:{source}'
        self.on_final = on_final
        self.connect_timeout = connect_timeout
        self.stop_timeout = stop_timeout
        self._ws = None
        self._reader = None
        self.finals = 0

    async def _connect(self):
        self._ws = await asyncio.wait_for(
            websockets.connect(self.url, max_size=1_048_576, ping_interval=20,
                               proxy=None),
            timeout=5.0)
        ws = self._ws
        await ws.send(json.dumps({
            'type': 'start', 'protocol': 2, 'source_id': self.source_id,
            'rate': 16000, 'channels': 1}))
        self._reader = asyncio.get_event_loop().create_task(self._read(ws))

    async def _read(self, ws):
        try:
            async for raw in ws:
                try:
                    message = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(message, dict) and message.get('type') == 'final':
                    self.finals += 1
                    self.on_final(str(message.get('text') or ''),
                                  int(message.get('start_offset') or 0),
                                  int(message.get('end_offset') or 0),
                                  self.source_id)
        except websockets.ConnectionClosed:
            pass

    async def feed(self, pcm: bytes) -> None:
        if self._ws is None:
            await self._connect()
        ws = self._ws
        assert ws is not None
        await ws.send(pcm)

    async def gap(self) -> None:
        if self._ws is not None:
            await self._ws.send(json.dumps({'type': 'gap'}))


    async def close(self) -> None:
        if self._ws is None:
            return
        try:
            await self._ws.send(json.dumps({'type': 'stop'}))
            await asyncio.wait_for(self._wait_stopped(), timeout=self.stop_timeout)
        except (asyncio.TimeoutError, websockets.ConnectionClosed, OSError):
            pass
        finally:
            if self._reader is not None:
                self._reader.cancel()
            await self._ws.close()

    async def _wait_stopped(self):
        ws = self._ws
        assert ws is not None
        while True:
            raw = await ws.recv()
            try:
                message = json.loads(raw)
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(message, dict) and message.get('type') == 'stopped':
                return


class RemoteSttSessionFactory:
    def __init__(self, url: str):
        self.url = url

    def open(self, session_id: str, source: str, on_final):
        session = RemoteSttSession(self.url, session_id, source, on_final)
        return _Connected(session)


class _Connected:
    def __init__(self, session: RemoteSttSession):
        self.session = session

    async def feed(self, pcm: bytes) -> None:
        await self.session.feed(pcm)

    async def gap(self) -> None:
        await self.session.gap()

    async def close(self) -> None:
        await self.session.close()
