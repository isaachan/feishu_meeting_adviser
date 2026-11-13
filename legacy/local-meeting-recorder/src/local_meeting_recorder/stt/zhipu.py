"""Zhipu GLM-ASR client (remote STT provider, stage 5.4).

POST {base}/paas/v4/audio/transcriptions  (Bearer auth, multipart/form-data)
  file   -> 16-bit PCM wav built from the mono float32 segment
  model  -> glm-asr-2512 (audio <= 30 s / 25 MB per request)
  prompt -> previous transcription context for cross-segment coherence
Retries: HTTP 429/5xx with bounded backoff; 4xx mapped to TranscriberError
reasons (auth/segment_too_large/request_rejected). Uses only the stdlib.
"""
from __future__ import annotations

import io
import json
import os
import time
import urllib.error
import urllib.request
import uuid
import wave

from .base import TranscriberError, TranscriptionResult

DEFAULT_BASE_URL = 'https://open.bigmodel.cn/api'
DEFAULT_MODEL = 'glm-asr-2512'
_RETRIES = 3
_BACKOFF_S = (0.5, 1.0, 2.0)
_MAX_AUDIO_SECONDS = 30


def _pcm_to_wav(pcm: bytes, rate: int) -> bytes:
    import array

    floats = array.array('f')
    floats.frombytes(pcm)
    samples = array.array('h', (int(max(-1.0, min(1.0, v)) * 32766)
                                for v in floats))
    out = io.BytesIO()
    with wave.open(out, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(samples.tobytes())
    return out.getvalue()


def _multipart(fields: dict[str, str], filename: str, payload: bytes,
               boundary: str) -> bytes:
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            (f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"'
             f'\r\n\r\n{value}\r\n').encode('utf-8'))
    parts.append(
        (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
         f'filename="{filename}"\r\nContent-Type: audio/wav\r\n\r\n').encode()
        + payload + b'\r\n')
    parts.append(f'--{boundary}--\r\n'.encode())
    return b''.join(parts)


class ZhipuASR:
    """Blocking file-upload ASR; one call per voice segment."""

    def __init__(self, *, api_key: str | None = None,
                 base_url: str | None = None, model: str | None = None,
                 timeout: float = 30.0, clock=time.monotonic,
                 sleep=time.sleep, urlopen=None):
        self.api_key = api_key or os.environ.get('GLM_ASR_API_KEY') \
            or os.environ.get('GLM_API_KEY')
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip('/')
        self.model = model or DEFAULT_MODEL
        self.timeout = timeout
        self._clock = clock
        self._sleep = sleep
        self._urlopen = urlopen or urllib.request.urlopen

    def transcribe(self, pcm: bytes, *, rate: int = 16000,
                   prompt: str | None = None,
                   request_id: str | None = None) -> TranscriptionResult:
        if not self.api_key:
            raise TranscriberError(
                'config', 'missing API key (GLM_ASR_API_KEY / GLM_API_KEY)')
        if len(pcm) // 4 > rate * _MAX_AUDIO_SECONDS:
            raise TranscriberError(
                'segment_too_large',
                f'segment exceeds {_MAX_AUDIO_SECONDS}s upload limit')
        request_id = request_id or uuid.uuid4().hex
        if not 6 <= len(request_id) <= 64:
            request_id = uuid.uuid4().hex  # API limit: 6..64 chars
        wav = _pcm_to_wav(pcm, rate)
        boundary = '----lmr' + uuid.uuid4().hex
        fields: dict[str, str] = {'model': self.model, 'request_id': request_id}
        if prompt:
            fields['prompt'] = prompt[:8000]
        body = _multipart(fields, 'segment.wav', wav, boundary)
        request = urllib.request.Request(
            f'{self.base_url}/paas/v4/audio/transcriptions', data=body,
            headers={'Authorization': f'Bearer {self.api_key}',
                     'Content-Type': f'multipart/form-data; boundary={boundary}'},
            method='POST')
        started = self._clock()
        last_error: str | None = None
        for attempt in range(_RETRIES):
            try:
                with self._urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode('utf-8'))
                latency = int((self._clock() - started) * 1000)
                return TranscriptionResult(text=payload.get('text', ''),
                                           request_id=payload.get('request_id', request_id),
                                           latency_ms=latency, model=self.model)
            except urllib.error.HTTPError as exc:
                detail = ''
                try:
                    detail = exc.read().decode('utf-8', 'replace')[:500]
                except Exception:  # noqa: BLE001
                    pass
                exc.close()
                if exc.code == 401:
                    raise TranscriberError(
                        'auth', f'zhipu asr 401: {detail}') from exc
                if exc.code == 413:
                    raise TranscriberError(
                        'segment_too_large', 'zhipu asr 413') from exc
                if exc.code >= 400 and exc.code < 500 and exc.code != 429:
                    raise TranscriberError(
                        'request_rejected',
                        f'zhipu asr {exc.code}: {detail}') from exc
                last_error = f'HTTP {exc.code}: {detail}'
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_error = f'network: {exc}'
            if attempt + 1 < _RETRIES:
                self._sleep(_BACKOFF_S[min(attempt, len(_BACKOFF_S) - 1)])
        raise TranscriberError('unavailable',
                               f'zhipu asr failed after {_RETRIES} attempts: '
                               f'{last_error}')
