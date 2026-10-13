from contextlib import redirect_stdout
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from feishu_meeting_adviser.__main__ import main
from feishu_meeting_adviser.config import load_config
from feishu_meeting_adviser.lifecycle import SessionManager
from feishu_meeting_adviser.models import Caption
from feishu_meeting_adviser.storage import StorageError, Store
from test_lifecycle import BotAPI, MID


class TranscriptPreviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config_path = Path(self.tmp.name)/'config.yaml'
        self.config_path.write_text('app:\n  id: synthetic\n  secret: synthetic-secret\nmeeting:\n  meeting_no: "012345678"\n')
        self.config_path.chmod(0o600)
        self.config = load_config(self.config_path)
        self.store = Store(self.config.data_directory/'source.sqlite3')
        self.addCleanup(self.store.close)
        self.api = BotAPI()
        SessionManager(self.store,self.config,self.api).join()
        self.store.ensure_meeting(MID,{})
        self.first = Caption(MID,'s:1','合成测试字幕一','2026-10-13T00:00:00Z',speaker_name='合成说话人')
        self.store.commit_page(MID,[self.first,replace(self.first,sentence_key='s:2',text='合成测试字幕二')],{})

    def command(self, *args):
        output = io.StringIO()
        with patch('feishu_meeting_adviser.__main__.FeishuClient',return_value=self.api),redirect_stdout(output):
            result = main(['--config',str(self.config_path),'transcripts',*args])
        self.assertEqual(result,0)
        self.assertEqual(self.api.event_calls,0)
        return [json.loads(line) for line in output.getvalue().splitlines()]

    def test_default_hides_text_and_speaker_without_consuming(self):
        checkpoint = self.store.checkpoint(MID)
        output = self.command()
        self.assertEqual(output,[{'recent_caption_count':2,'text_hidden':True}])
        self.assertEqual(self.store.checkpoint(MID),checkpoint)
        self.assertEqual(self.store.backlog(MID,'fake-qa'),2)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM consumers').fetchone()[0],0)
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM attempts').fetchone()[0],0)

    def test_explicit_preview_still_works_after_consumption_and_is_meeting_scoped(self):
        rows = self.store.read(MID,'fake-qa')
        self.store.acknowledge(MID,'fake-qa',rows[-1].sequence)
        self.store.ensure_meeting('other-meeting',{})
        self.store.commit_page('other-meeting',[replace(self.first,meeting_id='other-meeting',text='other meeting')],{})
        progress = tuple(self.store.db.execute('SELECT acknowledged,offered FROM consumers').fetchone())
        output = self.command('--show-text')
        self.assertEqual([row['text'] for row in output[1:]],['合成测试字幕一','合成测试字幕二'])
        self.assertEqual(output[1]['speaker'],'合成说话人')
        self.assertNotIn(MID,json.dumps(output))
        self.assertEqual(self.store.backlog(MID,'fake-qa'),0)
        self.assertEqual(tuple(self.store.db.execute('SELECT acknowledged,offered FROM consumers').fetchone()),progress)

    def test_recent_limit_and_empty_preview(self):
        self.assertEqual(self.command('--show-text','--limit','1')[1]['text'],'合成测试字幕二')
        self.store.ensure_meeting('empty',{})
        self.assertEqual(self.store.recent_captions('empty'),[])
        for limit in (0,1001,True):
            with self.assertRaises(StorageError):
                self.store.recent_captions(MID,limit=limit)
