import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from stream_stt_mock import VemediaParser, PCMChunker, MockSTT, ffmpeg_executable

SAMPLE = Path(__file__).resolve().parents[1] / 'material/double_click_20261008140038.vemedia'
SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/stream_stt_mock.py'


class ParserTests(unittest.TestCase):
    def test_split_at_arbitrary_byte_offsets(self):
        data = SAMPLE.read_bytes()[:500001]
        parser = VemediaParser()
        frames = []
        for offset in range(0, len(data), 97):
            frames += parser.feed(data[offset:offset+97])
        self.assertEqual(len(frames), 1299)
        self.assertEqual(parser.rate, 44100)
        self.assertEqual(parser.pts_discontinuities, 0)
        self.assertEqual(frames[0][1], -1024)
        self.assertGreater(parser.pending_bytes, 0)  # incomplete final record

    def test_bad_format_rejected(self):
        with self.assertRaisesRegex(ValueError, 'header'):
            VemediaParser().feed(b'\0' * 48)

    def test_chunk_duration_and_final_remainder(self):
        c = PCMChunker(200)
        self.assertEqual(c.feed(b'\0' * 6399), [])
        self.assertEqual([len(x) for x in c.feed(b'\0' * 4)], [6400])
        with self.assertRaisesRegex(ValueError, 'Unaligned'):
            c.flush()  # unaligned PCM must be rejected
        c.feed(b'\0')
        self.assertEqual(len(c.flush()), 4)

    def test_mock_returns_receipt_not_recognition(self):
        stt = MockSTT()
        stt.start()
        reply = stt.send_pcm(bytes(6400), 16000)[0]
        self.assertEqual(reply['type'], 'mock_receipt')
        self.assertIn('[MOCK]', reply['text'])
        self.assertEqual(stt.finish()[0]['audio_seconds'], .2)


class IntegrationTests(unittest.TestCase):
    def test_growing_file_produces_pcm_before_meeting_ends(self):
        try:
            exe = ffmpeg_executable()
        except RuntimeError:
            self.skipTest('ffmpeg/imageio-ffmpeg not installed')
        data = SAMPLE.read_bytes()
        with tempfile.TemporaryDirectory() as d:
            source = Path(d) / 'live.vemedia'
            output = Path(d) / 'result'
            source.write_bytes(data[:100000])
            proc = subprocess.Popen([sys.executable, str(SCRIPT), str(source), '--ffmpeg', exe,
                                     '--outdir', str(output), '--poll-ms', '30',
                                     '--missing-grace', '.25'], stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 5
                # The mock must receive PCM while the source still exists.
                while time.monotonic() < deadline:
                    if (output / 'events.jsonl').exists():
                        rows = [json.loads(x) for x in (output / 'events.jsonl').read_text().splitlines()]
                        if any(x['type'] == 'mock_receipt' for x in rows):
                            break
                    time.sleep(.05)
                else:
                    self.fail('No PCM delivered before end-of-file/meeting stop')
                with source.open('ab') as f:
                    f.write(data[100000:300001])  # leave a partial record
                time.sleep(.4)
                source.unlink()
                stdout, stderr = proc.communicate(timeout=10)
                self.assertEqual(proc.returncode, 0, stderr)
                rows = [json.loads(x) for x in (output / 'events.jsonl').read_text().splitlines()]
                end = rows[-1]
                self.assertEqual(end['reason'], 'source_missing_timeout')
                self.assertEqual(end['pts_discontinuities'], 0)
                self.assertGreater(end['trailing_incomplete_bytes'], 0)
                self.assertGreater(end['chunks'], 1)
                self.assertAlmostEqual((output / 'pcm_s16le_16k_mono.raw').stat().st_size / 32000,
                                       end['pcm_seconds'], places=4)
                receipts = [x for x in rows if x['type'] == 'mock_receipt']
                self.assertTrue(all(x['duration_s'] <= .2 for x in receipts))
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.communicate()


if __name__ == '__main__':
    unittest.main()
