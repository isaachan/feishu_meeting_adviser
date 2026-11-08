import argparse
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from stt_providers import MockSTT, LocalSherpaSTT, create_stt_session
from stream_stt_mock import ffmpeg_executable, run

MODEL = ROOT / 'models/sherpa-onnx-x-asr-480ms-streaming-zipformer-transducer-zh-en-punct-int8-2026-06-05'
SAMPLE = ROOT / 'material/double_click_20261008140038.vemedia'


class ProviderTests(unittest.TestCase):
    def test_provider_selection_and_validation(self):
        self.assertIsInstance(create_stt_session('mock'), MockSTT)
        with self.assertRaisesRegex(ValueError, 'local-model-dir'):
            create_stt_session('local')
        self.assertIsInstance(create_stt_session('local', model_dir=MODEL), LocalSherpaSTT)
        with self.assertRaisesRegex(ValueError, 'Unknown STT'):
            create_stt_session('no-such-provider')

    def test_injected_provider_receives_pcm_and_emits_transcript(self):
        try:
            exe = ffmpeg_executable()
        except RuntimeError:
            self.skipTest('FFmpeg is unavailable')

        class SyntheticSTT:
            def __init__(self):
                self.chunks = self.pcm_bytes = 0
            def start(self):
                pass
            def send_pcm(self, pcm, sample_rate):
                self.chunks += 1
                self.pcm_bytes += len(pcm)
                return [{'type': 'transcript', 'status': 'partial', 'text': 'injected', 'segment': 1}] if self.chunks == 1 else []
            def finish(self):
                return [{'type': 'transcript', 'status': 'final', 'text': 'injected', 'segment': 1}]
            def cancel(self):
                pass

        made = []
        def factory(provider, *, model_dir, local_threads):
            made.append((provider, model_dir, local_threads))
            return SyntheticSTT()

        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / 'snapshot.vemedia'
            source.write_bytes(SAMPLE.read_bytes()[:120000])
            args = argparse.Namespace(source=source, stt_provider='test', local_model_dir=None,
                                      local_threads=2, target_chunk_ms=200, poll_ms=100,
                                      max_chunk_wait_ms=300, missing_grace=10,
                                      outdir=Path(d) / 'out', ffmpeg=exe, replay=True)
            run(args, stt_factory=factory)
            self.assertEqual(made, [('test', None, 2)])
            rows = [json.loads(x) for x in (args.outdir / 'events.jsonl').read_text().splitlines()]
            self.assertEqual([r['status'] for r in rows if r['type'] == 'transcript'],
                             ['partial', 'final'])
            self.assertEqual(rows[-1]['reason'], 'completed_replay')

    def test_start_failure_cancels_injected_provider(self):
        class BrokenSTT:
            chunks = pcm_bytes = 0
            cancelled = False
            def start(self):
                raise RuntimeError('startup failed')
            def cancel(self):
                self.cancelled = True
        session = BrokenSTT()
        with tempfile.TemporaryDirectory() as d:
            args = argparse.Namespace(source=SAMPLE, stt_provider='test', local_model_dir=None,
                                      local_threads=2, target_chunk_ms=200, poll_ms=100,
                                      max_chunk_wait_ms=300, missing_grace=10,
                                      outdir=Path(d)/'out', ffmpeg=None, replay=True)
            with self.assertRaisesRegex(RuntimeError, 'startup failed'):
                run(args, stt_factory=lambda *a, **k: session)
            self.assertTrue(session.cancelled)
            rows = [json.loads(x) for x in (args.outdir/'events.jsonl').read_text().splitlines()]
            self.assertEqual(rows[-1]['reason'], 'error')

    def test_full_local_pipeline_with_synthetic_vemedia(self):
        if not (MODEL / 'tokens.txt').is_file():
            self.skipTest('Optional local model has not been downloaded')
        try:
            import sherpa_onnx
            import numpy
            exe = ffmpeg_executable()
        except (ImportError, RuntimeError):
            self.skipTest('Optional local STT dependencies are unavailable')
        encoded = subprocess.run([exe, '-hide_banner', '-loglevel', 'error',
                                  '-i', str(MODEL / 'test_wavs/0.wav'), '-ar', '44100', '-ac', '2',
                                  '-c:a', 'aac', '-f', 'adts', 'pipe:1'], capture_output=True,
                                 check=True).stdout
        payloads = []
        offset = 0
        while offset + 7 <= len(encoded):
            self.assertEqual(encoded[offset], 255)
            size = ((encoded[offset+3] & 3) << 11) | (encoded[offset+4] << 3) | (encoded[offset+5] >> 5)
            payloads.append(encoded[offset+7:offset+size])
            offset += size
        self.assertEqual(offset, len(encoded))
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / 'speech.vemedia'
            with source.open('wb') as output:
                output.write(SAMPLE.read_bytes()[:151])  # lark/mdat, SSTR and matching 44.1kHz AAC SIFO
                for i, payload in enumerate(payloads):
                    output.write((23+len(payload)).to_bytes(4, 'big') + b'ES  ' +
                                 b'\x00\x01\x01' + ((i-1)*1024).to_bytes(8, 'big', signed=True) +
                                 b'\x00'*4 + payload)
            outdir = Path(d) / 'out'
            args = argparse.Namespace(source=source, stt_provider='local', local_model_dir=MODEL,
                                      local_threads=2, target_chunk_ms=200, poll_ms=100,
                                      max_chunk_wait_ms=300, missing_grace=10,
                                      outdir=outdir, ffmpeg=exe, replay=True)
            run(args)
            rows = [json.loads(x) for x in (outdir / 'events.jsonl').read_text().splitlines()]
            finals = [x['text'] for x in rows if x['type'] == 'transcript' and x['status'] == 'final']
            self.assertTrue(any('Monday' in text and '星期三' in text for text in finals), finals)
            self.assertEqual(rows[-1]['pts_discontinuities'], 0)

    def test_real_local_streaming_model_when_installed(self):
        if not (MODEL / 'tokens.txt').is_file():
            self.skipTest('Optional local model has not been downloaded')
        try:
            import sherpa_onnx
            import numpy
        except ImportError:
            self.skipTest('Optional sherpa-onnx and numpy not installed')
        session = create_stt_session('local', model_dir=MODEL, local_threads=2)
        session.start()
        emitted = []
        with wave.open(str(MODEL / 'test_wavs/0.wav'), 'rb') as audio:
            self.assertEqual((audio.getframerate(), audio.getnchannels(), audio.getsampwidth()),
                             (16000, 1, 2))
            while chunk := audio.readframes(3200):
                emitted.extend(session.send_pcm(chunk, 16000))
        emitted.extend(session.finish())
        self.assertTrue(any(r['status'] == 'partial' for r in emitted))
        finals = [r['text'] for r in emitted if r['status'] == 'final']
        self.assertTrue(any('Monday' in x and '星期三' in x for x in finals), finals)


if __name__ == '__main__':
    unittest.main()
