"""Check the module-owned STT scripts and optional local inference assets."""
from pathlib import Path
import subprocess
import sys
import unittest
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from stream_stt_mock import PCMChunker, VemediaParser
from stt_providers import create_stt_session

MODEL = ROOT / 'models/sherpa-onnx-x-asr-480ms-streaming-zipformer-transducer-zh-en-punct-int8-2026-06-05'


class LocalSTTTests(unittest.TestCase):
    def test_bridge_runs_in_its_own_environment(self):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/collect-local.py'), '--help'],
                                cwd=ROOT.parent, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--download-model', result.stdout)

    def test_parser_and_pcm_chunking_need_no_external_module(self):
        with self.assertRaisesRegex(ValueError, 'header'):
            VemediaParser().feed(b'\0' * 48)
        chunks = PCMChunker(200)
        self.assertEqual(chunks.feed(b'\0' * 6402), [b'\0' * 6400])
        self.assertEqual(chunks.flush(), b'\0' * 2)

    def test_optional_model_transcribes_its_bundled_sample(self):
        if not (MODEL / 'tokens.txt').is_file():
            self.skipTest('optional STT model not downloaded')
        try:
            import sherpa_onnx
            import numpy
        except ImportError:
            self.skipTest('install meeting-listener[stt] to run local inference')
        session = create_stt_session('local', model_dir=MODEL, local_threads=2)
        session.start()
        emitted = []
        with wave.open(str(MODEL / 'test_wavs/0.wav'), 'rb') as audio:
            while chunk := audio.readframes(3200):
                emitted.extend(session.send_pcm(chunk, 16000))
        emitted.extend(session.finish())
        finals = [row['text'] for row in emitted if row['status'] == 'final']
        self.assertTrue(any('Monday' in text and '星期三' in text for text in finals), finals)
