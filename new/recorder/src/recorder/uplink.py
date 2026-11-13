"""WS uplink client per CONTRACT_RECORDER_STREAM v1 (one connection per source).

Sequencing uses the capture-time per-source frame sequence (stable across
reconnects, which is what the contract's idempotency dedup requires). Unacked
frames are buffered in a bounded deque (retransmission window); overflow is
counted explicitly, never silently stretched. Ctrl-C never fabricates
stream_end; clean stops (--duration, orderly shutdown) do.
"""
import json
import queue
import struct
import threading
import time
from collections import deque

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from .audio.frames import AudioFrame, SourceEvent

MAX_FRAME_BYTES = 1_048_576
FATAL_AUTH_CODES = ('bad_token', 'bad_binding', 'bad_protocol', 'bad_message')
BACKOFF_S = (0.5, 1, 2, 5, 10)
_SEND_BATCH = 32   # frames per loop cycle: ≥10x realtime for ~30ms frames
_MAX_INFLIGHT = 2048  # send-ahead bound (frames beyond the acked cursor)


def _log(source_id, event, **fields):
    print(f'[uplink:{source_id}] {event} ' + json.dumps(fields, ensure_ascii=False),
          flush=True)


class UplinkExit(Exception):
    def __init__(self, code, reason):
        self.code, self.reason = code, reason
        super().__init__(reason)


def float32_to_int16(data: bytes) -> bytes:
    import array
    floats = array.array('f')
    floats.frombytes(data)
    out = array.array('h', (int(max(-1.0, min(1.0, v)) * 32766) for v in floats))
    return out.tobytes()


class SourceUplink:
    """Streams one audio source to the advisor ingest endpoint."""

    def __init__(self, source_id, *, session_id, event, url,
                 token, speaker=None, window=4096, clock=time.time,
                 reconnect_limit=8, handshake_timeout=10):
        self.source_id = source_id
        self.controls = queue.Queue()
        self.session_id = session_id
        self.event = event
        self.url = url
        self.token = token
        self.speaker = speaker
        self.window = window
        self.clock = clock
        self.reconnect_limit = reconnect_limit
        self.handshake_timeout = handshake_timeout
        self.buffer: deque[tuple[int, bytes]] = deque(maxlen=window)
        self.acked = 0
        self._sent_upto = 0  # highest sequence sent on the CURRENT connection
        self.last_seq = 0
        self.reconnects = 0
        self.dropped_frames = 0
        self.sent_frames = 0
        self.exit_code = 0
        self.exit_reason = ''
        self.stopped = threading.Event()
        self._thread = None

    # -- capture side ----------------------------------------------------------

    def offer(self, item) -> None:
        """CaptureSession subscriber callback; never blocks the producer."""
        if self.stopped.is_set():
            return
        if isinstance(item, AudioFrame):
            if item.frames == 0:
                return
            pcm = float32_to_int16(item.data)
            if len(self.buffer) == self.window:
                self.dropped_frames += 1
            self.last_seq = max(self.last_seq, item.seq)
            self.buffer.append((item.seq, pcm))
        elif isinstance(item, SourceEvent):
            if item.type in ('gap_begin',):
                self.controls.put_nowait({'type': 'gap'})

    # -- streaming ---------------------------------------------------------------

    def start(self):
        self._thread = threading.Thread(target=self._run, name=f'uplink-{self.source_id}',
                                        daemon=True)
        self._thread.start()

    def join(self, timeout=10):
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def stop_clean(self):
        """Clean stop: request stream_end (used for --duration / orderly exit)."""
        self.controls.put({'type': 'stream_end'})
        self.stopped.wait(timeout=15)

    def stop_interrupt(self):
        """Ctrl-C: no stream_end, buffer preserved for a later resume."""
        self.stopped.set()

    def _run(self):
        attempts = 0
        resume = False
        while not self.stopped.is_set():
            try:
                self._one_connection(resume)
                return
            except _Retry as retry:
                resume = retry.resume
                _log(self.source_id, 'retry', stale_sequence=True)
                continue
            except UplinkExit as exc:
                self.exit_code, self.exit_reason = exc.code, exc.reason
                _log(self.source_id, 'exit', code=exc.code, reason=exc.reason)
                self.stopped.set()
                return
            except (OSError, WebSocketException) as exc:
                _log(self.source_id, 'connection_lost',
                     error=exc.__class__.__name__, attempt=attempts + 1)
            attempts += 1
            self.reconnects += 1
            if attempts > self.reconnect_limit:
                self.exit_code, self.exit_reason = 3, 'retries exhausted'
                _log(self.source_id, 'exit', code=3, reason='retries exhausted')
                self.stopped.set()
                return
            backoff = BACKOFF_S[min(attempts - 1, len(BACKOFF_S) - 1)]
            _log(self.source_id, 'backoff', seconds=backoff,
                 buffered=len(self.buffer))
            self.stopped.wait(backoff)

    def _one_connection(self, resume):
        first_sequence = self.acked + 1
        _log(self.source_id, 'connecting', url=self.url, resume=resume,
             first_sequence=max(first_sequence, 1), buffered=len(self.buffer))
        with connect(self.url, open_timeout=self.handshake_timeout,
                     close_timeout=5, max_size=MAX_FRAME_BYTES, proxy=None) as ws:
            start = {'type': 'start', 'protocol': 1, 'session_id': self.session_id,
                     'stream_token': self.token, 'source': self.source_id,
                     'event': self.event, 'resume': resume,
                     'first_sequence': max(first_sequence, 1),
                     'wall_clock_at': _iso(self.clock())}
            if self.source_id == 'mic':
                start['speaker'] = self.speaker or '本机'
            ws.send(json.dumps(start))
            message = _recv_json(ws, timeout=self.handshake_timeout)
            if message is None:
                raise ConnectionLost('no greeting')
            if message.get('type') == 'error':
                self._raise_for_code(message.get('code'), message.get('message'))
            if message.get('type') != 'started':
                raise UplinkExit(3, f'unexpected greeting: {message.get("type")}')
            server_acked = int(message.get('acked_sequence') or 0)
            self._trim(server_acked)
            self._sent_upto = self.acked
            _log(self.source_id, 'started', stream_id=message.get('stream_id'),
                 server_acked=server_acked)
            while not self.stopped.is_set():
                # Short control wait: a 0.1s block here throttled the whole
                # loop to ~1 frame per 0.26s (≈25% realtime, 2026-11-13
                # real-meeting finding); batch-send + brief polls instead.
                try:
                    control = self.controls.get(timeout=0.02)
                except queue.Empty:
                    control = None
                if control is not None:
                    if control.get('type') == 'stream_end':
                        self._stream_end(ws)
                        return
                    if control.get('type') == 'gap':
                        ws.send(json.dumps({'type': 'gap'}))
                sent = 0
                # Frames stay in the buffer until acked (contract §4.2: buffer
                # everything un-acked); a mid-batch disconnect therefore loses
                # nothing — the reconnect resends from acked+1. Scan to the
                # first unsent frame: the head may still be awaiting its ack.
                index = 0
                while index < len(self.buffer) and sent < _SEND_BATCH:
                    seq, pcm = self.buffer[index]
                    if seq <= self._sent_upto:
                        index += 1
                        continue
                    if seq - self.acked > _MAX_INFLIGHT:
                        break  # too far ahead of the acked cursor; wait
                    if len(pcm) + 4 > MAX_FRAME_BYTES:
                        del self.buffer[index]
                        self.dropped_frames += 1
                        continue
                    ws.send(struct.pack('<I', seq) + pcm)
                    self.sent_frames += 1
                    self._sent_upto = seq
                    sent += 1
                    index += 1
                for _ in range(4):
                    message = _recv_json(ws, timeout=0.005)
                    if message is None:
                        break
                    kind = message.get('type')
                    if kind == 'ack':
                        self._trim(int(message.get('sequence') or 0))
                    elif kind == 'error':
                        code = message.get('code')
                        if code == 'stale_sequence':
                            # server dropped our window: restart capture epoch
                            self.buffer.clear()
                            self.acked = 0
                            self._sent_upto = 0
                            raise _Retry(resume=False)
                        self._raise_for_code(code, message.get('message'))
                    elif kind == 'stopped':
                        _log(self.source_id, 'stopped_by_server',
                             acked=message.get('acked_sequence'), stats=self.stats())
                        self.stopped.set()
                        return

    def _stream_end(self, ws):
        last = self.last_seq
        for seq, _pcm in self.buffer:
            last = max(last, seq)
        for seq, pcm in self.buffer:
            if seq <= self.acked:
                continue
            ws.send(struct.pack('<I', seq) + pcm)
            self.sent_frames += 1
        self._sent_upto = max(self._sent_upto, last)
        _log(self.source_id, 'stream_end', last_sequence=max(last, self.acked))
        ws.send(json.dumps({'type': 'stream_end', 'last_sequence': max(last, self.acked)}))
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            message = _recv_json(ws, timeout=1.0)
            if message is None:
                continue
            if message.get('type') == 'stopped':
                self.acked = max(self.acked, int(message.get('acked_sequence') or 0))
                _log(self.source_id, 'stopped_by_server',
                     acked=self.acked, stats=self.stats())
                break
            if message.get('type') == 'ack':
                self._trim(int(message.get('sequence') or 0))
        self.stopped.set()

    def _trim(self, acked):
        self.acked = max(self.acked, acked)
        while self.buffer and self.buffer[0][0] <= self.acked:
            self.buffer.popleft()

    def _raise_for_code(self, code, detail):
        if code in FATAL_AUTH_CODES:
            raise UplinkExit(2, f'{code}: {detail}')
        if code == 'session_ended':
            raise UplinkExit(3, 'session ended on server')
        raise OSError(f'{code}: {detail}')  # transient: reconnect with backoff

    def stats(self):
        return {'source': self.source_id, 'sent_frames': self.sent_frames,
                'acked_sequence': self.acked, 'buffered': len(self.buffer),
                'reconnects': self.reconnects, 'dropped_frames': self.dropped_frames}


class _Retry(Exception):
    def __init__(self, *, resume):
        self.resume = resume
        super().__init__('retry')


def _recv_json(ws, timeout):
    try:
        raw = ws.recv(timeout=timeout)
    except TimeoutError:
        return None
    except WebSocketException as exc:
        raise ConnectionLost(str(exc)) from exc
    if isinstance(raw, bytes):
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


class ConnectionLost(OSError):
    """Peer closed the connection; the uplink reconnects with backoff."""


def _iso(now: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(now, timezone.utc).isoformat()
