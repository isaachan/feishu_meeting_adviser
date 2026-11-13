import json
import os
import struct
import unittest
import urllib.error
from unittest import mock

from local_meeting_recorder.audio import AudioFrame
from local_meeting_recorder.stt import (
    Segment,
    TranscriberError,
    TranscriptionResult,
    VadSegmenter,
    ZhipuASR,
    create_transcriber,
)
from local_meeting_recorder.stt.zhipu import _pcm_to_wav

RATE = 16000


def tone(count: int, amp: float = 0.2) -> bytes:
    return struct.pack('<f', amp) * count


def frame(source: str, offset: int, data: bytes) -> AudioFrame:
    return AudioFrame(source, 0, offset, RATE, 1, data, 0.0)


def feed_chunks(segmenter: VadSegmenter, data: bytes, start: int,
                chunk: int = 1600):
    out: list[Segment] = []
    for i in range(0, len(data) // 4, chunk):
        piece = data[i * 4:(i + chunk) * 4]
        out += segmenter.feed(frame(segmenter.source_id, start + i, piece))
    return out


class SegmenterTests(unittest.TestCase):
    def test_one_utterance_emits_single_segment(self):
        seg = VadSegmenter('mic')
        audio = tone(16000, 0.2) + tone(16000, 0.0)  # 1s speech + 1s silence
        segments = feed_chunks(seg, audio, 0) + seg.flush()
        self.assertEqual(len(segments), 1)
        only = segments[0]
        self.assertEqual(only.source_id, 'mic')
        self.assertLessEqual(only.start_offset, 200 * RATE // 1000 + 1)
        self.assertGreaterEqual(only.end_offset, RATE)  # speech fully inside
        self.assertFalse(only.max_forced)
        self.assertGreaterEqual(only.speech_ms, 250)

    def test_short_blip_dropped(self):
        seg = VadSegmenter('mic')
        audio = tone(1600, 0.2) + tone(16000, 0.0)  # 100ms blip
        segments = feed_chunks(seg, audio, 0) + seg.flush()
        self.assertEqual(segments, [])

    def test_two_utterations_separated_by_silence(self):
        seg = VadSegmenter('mic')
        audio = (tone(8000, 0.2) + tone(24000, 0.0)   # 0.5s speech, 1.5s gap
                 + tone(8000, 0.2) + tone(8000, 0.0))
        segments = feed_chunks(seg, audio, 0) + seg.flush()
        self.assertEqual(len(segments), 2)
        self.assertEqual([s.index for s in segments], [1, 2])
        self.assertLess(segments[0].end_offset, segments[1].start_offset)

    def test_continuous_speech_force_cut_at_max(self):
        seg = VadSegmenter('loopback', max_segment_ms=1000)  # small cap
        audio = tone(32000, 0.2)  # 2s continuous
        segments = feed_chunks(seg, audio, 0) + seg.flush()
        self.assertGreaterEqual(len(segments), 2)
        self.assertTrue(segments[0].max_forced)
        # contiguous coverage: no lost samples between cuts
        cursor = 0
        for s in segments:
            self.assertEqual(s.start_offset, cursor)
            cursor = s.end_offset
        self.assertEqual(cursor, 32000)

    def test_gap_closes_open_segment(self):
        seg = VadSegmenter('mic')
        out = feed_chunks(seg, tone(8000, 0.2), 0)  # 0.5s speech, still open
        self.assertEqual(out, [])
        out = seg.feed_gap()
        self.assertEqual(len(out), 1)
        self.assertLessEqual(out[0].end_offset, 8000 + 400)

    def test_discontinuity_without_gap_resets_cleanly(self):
        seg = VadSegmenter('mic')
        out = feed_chunks(seg, tone(3200, 0.2), 0)
        # stream jumps forward without feed_gap()
        out += feed_chunks(seg, tone(3200, 0.2), 100000)
        out += seg.flush()
        self.assertTrue(all(s.end_offset > s.start_offset for s in out))

    def test_low_level_noise_not_detected_as_speech(self):
        seg = VadSegmenter('mic')
        audio = tone(32000, 0.005) + tone(4800, 0.2) + tone(16000, 0.005)
        segments = feed_chunks(seg, audio, 0) + seg.flush()
        self.assertEqual(len(segments), 1)
        only = segments[0]
        self.assertGreaterEqual(only.start_offset, 32000 - RATE // 5 - 1)
        self.assertLessEqual(only.end_offset, 36800 + RATE // 5 + 1)

    def test_stereo_rejected(self):
        seg = VadSegmenter('mic')
        stereo = AudioFrame('mic', 1, 0, RATE, 2, b'\0' * 8, 0.0)
        with self.assertRaises(ValueError):
            seg.feed(stereo)


class FakeResponse:
    def __init__(self, payload: dict, status: int = 200):
        self._payload = json.dumps(payload).encode()
        self.status = status

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ZhipuASRTests(unittest.TestCase):
    def make(self, **kwargs):
        defaults = dict(api_key='test-key', clock=lambda: 0.0, sleep=lambda s: None)
        defaults.update(kwargs)
        return ZhipuASR(**defaults)

    def test_success_returns_text_and_request_shape(self):
        calls = []

        def urlopen(request, timeout=None):
            calls.append(request)
            return FakeResponse({'text': '你好会议', 'request_id': 'rid-1'})

        result = self.make(urlopen=urlopen).transcribe(
            tone(16000), prompt='上一句')
        self.assertIsInstance(result, TranscriptionResult)
        self.assertEqual(result.text, '你好会议')
        request = calls[0]
        self.assertEqual(request.full_url,
                         'https://open.bigmodel.cn/api/paas/v4/audio/transcriptions')
        self.assertEqual(request.get_header('Authorization'), 'Bearer test-key')
        body = request.data.decode('utf-8', 'replace')
        self.assertIn('name="model"', body)
        self.assertIn('glm-asr-2512', body)
        self.assertIn('name="prompt"', body)
        self.assertIn('上一句', body)
        self.assertIn('name="request_id"', body)
        wav_start = request.data.find(b'RIFF')
        self.assertGreater(wav_start, 0)

    def test_wav_encoding(self):
        wav = _pcm_to_wav(tone(100, 0.5), RATE)
        self.assertTrue(wav.startswith(b'RIFF'))
        import wave as wave_mod
        with wave_mod.open(__import__('io').BytesIO(wav), 'rb') as wf:
            self.assertEqual((wf.getframerate(), wf.getnchannels(),
                              wf.getnframes()), (RATE, 1, 100))

    def test_retry_on_429_then_success(self):
        attempts = []

        def urlopen(request, timeout=None):
            attempts.append(1)
            if len(attempts) == 1:
                raise urllib.error.HTTPError(
                    request.full_url, 429, 'rate', {}, io_none())
            return FakeResponse({'text': 'ok'})

        sleeps = []
        asr = self.make(urlopen=urlopen, sleep=sleeps.append)
        self.assertEqual(asr.transcribe(tone(1600)).text, 'ok')
        self.assertEqual(len(attempts), 2)
        self.assertEqual(sleeps, [0.5])

    def test_401_no_retry(self):
        def urlopen(request, timeout=None):
            raise urllib.error.HTTPError(
                request.full_url, 401, 'unauthorized', {}, io_none())

        with self.assertRaises(TranscriberError) as ctx:
            self.make(urlopen=urlopen).transcribe(tone(1600))
        self.assertEqual(ctx.exception.reason, 'auth')

    def test_network_failure_exhausts_retries(self):
        attempts = []

        def urlopen(request, timeout=None):
            attempts.append(1)
            raise urllib.error.URLError('boom')

        with self.assertRaises(TranscriberError) as ctx:
            self.make(urlopen=urlopen, sleep=lambda s: None).transcribe(
                tone(1600))
        self.assertEqual(ctx.exception.reason, 'unavailable')
        self.assertEqual(len(attempts), 3)

    def test_missing_key_is_config_error(self):
        env = {'GLM_ASR_API_KEY': '', 'GLM_API_KEY': ''}
        with mock.patch.dict(os.environ, env):
            with self.assertRaises(TranscriberError) as ctx:
                ZhipuASR().transcribe(tone(1600))
        self.assertEqual(ctx.exception.reason, 'config')

    def test_oversize_segment_rejected_before_network(self):
        def urlopen(request, timeout=None):  # pragma: no cover - must not run
            raise AssertionError('should not call network')

        with self.assertRaises(TranscriberError) as ctx:
            self.make(urlopen=urlopen).transcribe(tone(RATE * 31))
        self.assertEqual(ctx.exception.reason, 'segment_too_large')

    def test_overlong_request_id_replaced(self):
        def urlopen(request, timeout=None):
            return FakeResponse({'text': 'ok'})

        result = self.make(urlopen=urlopen).transcribe(
            tone(1600), request_id='x' * 100)
        self.assertTrue(6 <= len(result.request_id) <= 64)


def io_none():
    import io
    return io.BytesIO(b'{}')


class CreateTranscriberTests(unittest.TestCase):
    def test_remote_default_zhipu(self):
        asr = create_transcriber({'stt': 'remote', 'api_key': 'k'})
        self.assertIsInstance(asr, ZhipuASR)
        self.assertEqual(asr.api_key, 'k')

    def test_unknown_modes_rejected(self):
        for config in ({'stt': 'local'}, {'stt': 'magic'}, {'stt': 'remote',
                                                            'provider': 'x'}):
            with self.assertRaises(TranscriberError):
                create_transcriber(config)


if __name__ == '__main__':
    unittest.main()
