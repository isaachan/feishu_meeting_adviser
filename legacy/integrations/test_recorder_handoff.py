"""Fake-recorder to advisor end-to-end flow over the frozen handoff contract."""
from contextlib import redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from feishu_meeting_adviser.__main__ import main as advisor_main
from feishu_meeting_adviser.adapters.persistence.adviser_store import AdviserStore
from feishu_meeting_adviser.config import load_config
from feishu_meeting_adviser.llm import Decision
from local_meeting_recorder.__main__ import main as recorder_main


class RecorderHandoffTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        directory = self.root / 'advisor'
        directory.mkdir()
        self.config_path = directory / 'config.yaml'
        self.config_path.write_text('app:\n  id: synthetic\n  secret: synthetic-secret\n'
                                    'storage:\n  directory: ../data\n'
                                    'llm:\n  backend: opencode\n  batch_seconds: 0\n')
        self.config_path.chmod(0o600)
        self.config = load_config(self.config_path)
        self.data_root = self.config.data_directory
        self.script = self.root / 'script.jsonl'
        self.script.write_text('\n'.join(json.dumps(row, ensure_ascii=False) for row in (
            {'speaker': '张三', 'text': '这个方案的风险是什么？', 'delay_ms': 0},
            {'speaker': '李四', 'text': '主要是集成的回滚路径。', 'delay_ms': 0},
        )) + '\n', encoding='utf-8')

    def run_advisor(self, *args):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            code = advisor_main(['--config', str(self.config_path), *args])
        return code, output.getvalue()

    def run_recorder(self, mid):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(io.StringIO()):
            code = recorder_main(['fake', str(self.script), '--session', mid,
                                  '--data-root', str(self.data_root), '--speed', '100'])
        return code, json.loads(output.getvalue())

    def test_replayed_script_reaches_a_saved_local_decision(self):
        code, output = self.run_advisor('session-new', '--title', '集成会议')
        self.assertEqual(code, 0)
        mid = json.loads(output)['session_id']

        code, summary = self.run_recorder(mid)
        self.assertEqual((code, summary['stopped_reason']), (0, 'completed'))
        stream = summary['stream_id']
        self.assertEqual(summary['events'], 3)  # two captions + stream_end

        class Backend:
            context_overhead_characters = 0
            snapshot = None

            def decide(self, snapshot, role):
                Backend.snapshot = snapshot
                nonce = stream.rsplit(':', 1)[-1][:8]
                return Decision('none', (), '主要风险是回滚路径，建议先补一次演练。',
                                (f'u-{nonce}-000001',))

            def diagnostic_metadata(self):
                return {}

            def close(self):
                pass

        with patch('feishu_meeting_adviser.adviser.create_backend', return_value=Backend()):
            code, output = self.run_advisor('consume', mid)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)['decisions'], 1)
        speakers = [item['speaker_name'] for item in Backend.snapshot['captions']]
        self.assertEqual(speakers, ['张三', '李四'])

        data = AdviserStore(self.data_root / 'adviser.sqlite3')
        try:
            rows = data.list_replies(mid, show_text=True)
            self.assertEqual([(row['channel'], row['state']) for row in rows],
                             [('local', 'sent')])
            self.assertEqual(rows[0]['text'], '主要风险是回滚路径，建议先补一次演练。')
        finally:
            data.close()

        ack = self.data_root / 'recorder' / 'ack' / mid / (stream + '.json')
        self.assertEqual(json.loads(ack.read_text()), {'acked_sequence': 3})

        # Rerunning consume stays idempotent, and the ended session refuses restart.
        with patch('feishu_meeting_adviser.adviser.create_backend', return_value=Backend()):
            code, output = self.run_advisor('consume', mid)
        self.assertEqual(code, 0)
        code, _ = self.run_advisor('session-end', mid)
        self.assertEqual(code, 0)
        data = AdviserStore(self.data_root / 'adviser.sqlite3')
        try:
            self.assertEqual(data.require_local_session(mid)['phase'], 'ended')
        finally:
            data.close()
        code, _ = self.run_advisor('consume', mid)
        self.assertEqual(code, 1)

    def test_unterminated_stream_blocks_auto_end_until_forced(self):
        code, output = self.run_advisor('session-new')
        self.assertEqual(code, 0)
        mid = json.loads(output)['session_id']
        # A stream without stream_end models an interrupted recorder.
        stream = f'{mid}:interrupted'
        segment = self.data_root / 'recorder' / 'spool' / mid / stream
        segment.mkdir(mode=0o700, parents=True)
        segment.joinpath('000001.jsonl').write_text(
            json.dumps({'schema_version': 1, 'session_id': mid, 'stream_id': stream,
                        'sequence': 1, 'event_id': f'{stream}:1', 'kind': 'caption',
                        'utterance_id': 'u-000001', 'revision': 1, 'text': '中断前的最后一句',
                        'speaker_label': None, 'start_offset_ms': 1000,
                        'end_offset_ms': 1500,
                        'wall_clock_at': '2026-11-08T10:00:00+00:00'}, ensure_ascii=False)
            + '\n', encoding='utf-8')
        code, _ = self.run_advisor('session-end', mid)
        self.assertEqual(code, 0)
        data = AdviserStore(self.data_root / 'adviser.sqlite3')
        try:
            self.assertEqual(data.require_local_session(mid)['phase'], 'draining')
        finally:
            data.close()
        code, _ = self.run_advisor('session-end', mid, '--force')
        self.assertEqual(code, 0)
        data = AdviserStore(self.data_root / 'adviser.sqlite3')
        try:
            self.assertEqual(data.require_local_session(mid)['phase'], 'ended')
        finally:
            data.close()


if __name__ == '__main__':
    unittest.main()
