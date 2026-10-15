from pathlib import Path
from dataclasses import replace
import io
import json
from types import SimpleNamespace
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from feishu_meeting_adviser.config import load_config
from feishu_meeting_adviser.llm import Decision
from meeting_adviser_harness.adapters import MaterialAdapter
from meeting_adviser_harness.__main__ import (
    Harness, _HarnessOutput, _handle_line, _read_text, _watch_events,
)


class FakeHarness:
    def __init__(self):
        self.speaker = '默认用户'
        self.calls = []
    def submit(self,speaker,text):
        self.calls.append((speaker,text))
    def display(self,value):
        self.calls.append(value)
    def context(self):
        self.calls.append('context')
    def restart(self,*,reset):
        self.calls.append(('restart',reset))


class HarnessAdapterTests(unittest.TestCase):
    def test_default_output_hides_background_logs_but_saves_every_event(self):
        with tempfile.TemporaryDirectory() as directory:
            stdout,stderr = io.StringIO(),io.StringIO()
            output = _HarnessOutput(Path(directory),stdout=stdout,stderr=stderr)
            events = [
                {'kind':'status','model_state':'running','model_error':None},
                {'kind':'diagnostic','model_stage':'generation'},
                {'kind':'model_decision','control':'none','reply':None},
                {'kind':'reply','status':'allowed','text':'最终回答','uuid':'synthetic'},
            ]
            for event in events:
                output(json.dumps(event,ensure_ascii=False))
            self.assertEqual(stdout.getvalue().strip(),'会参谋：最终回答')
            saved = [json.loads(line) for line in output.path.read_text().splitlines()]
            self.assertEqual(saved,events)

    def test_watch_follows_new_events_and_log_rotation(self):
        with tempfile.TemporaryDirectory() as directory:
            output = _HarnessOutput(Path(directory),stdout=io.StringIO())
            first = {'kind':'status','model_state':'waiting'}
            second = {'kind':'status','model_state':'running'}
            third = {'kind':'model_decision','control':'none','reply':None}
            output(json.dumps(first))
            stop,seen = threading.Event(),[]
            watcher = threading.Thread(target=_watch_events,
                args=(output.path,stop,seen.append),kwargs={'poll_seconds':.01})
            watcher.start()
            deadline = time.monotonic()+1
            while time.monotonic()<deadline and len(seen)<1:
                time.sleep(.01)
            output(json.dumps(second))
            deadline = time.monotonic()+1
            while time.monotonic()<deadline and len(seen)<2:
                time.sleep(.01)
            output.path.replace(Path(str(output.path)+'.manual'))
            output(json.dumps(third))
            deadline = time.monotonic()+1
            while time.monotonic()<deadline and len(seen)<3:
                time.sleep(.01)
            stop.set()
            watcher.join(1)
            self.assertFalse(watcher.is_alive())
            self.assertEqual([json.loads(line) for line in seen],[first,second,third])

    def test_multi_speaker_and_sticky_speaker_input(self):
        harness = FakeHarness()
        self.assertTrue(_handle_line(harness,'张三：第一段发言'))
        self.assertTrue(_handle_line(harness,'/speaker 李四'))
        self.assertTrue(_handle_line(harness,'第二段发言'))
        self.assertFalse(_handle_line(harness,'/quit'))
        self.assertEqual(harness.calls[0],('张三','第一段发言'))
        self.assertEqual(harness.calls[-1],('李四','第二段发言'))

    def test_plain_description_does_not_require_oauth(self):
        config = SimpleNamespace(timeout_seconds=10,request_seconds=.7,
            credentials=SimpleNamespace(app_id='x',app_secret='y'),meeting_no='012345678')
        session = {'joined_at':100}
        client = MaterialAdapter(
            config, Path('/unused'), '只有本地文字', session, threading.Event())
        self.assertIsNone(client.oauth_manager)
        event = client.event('harness','harness')
        self.assertEqual(event['description_rich'],'只有本地文字')
        self.assertEqual(event['vchat']['meeting_url'],'https://vc.feishu.cn/j/012345678')

    def test_description_file_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real = root/'real.md'
            real.write_text('content')
            link = root/'link.md'
            link.symlink_to(real)
            with self.assertRaisesRegex(Exception,'description file'):
                _read_text(link,'description file')

    def test_real_runner_accepts_new_input_while_model_is_busy_and_rechecks_reply(self):
        class Backend:
            context_overhead_characters = 0
            def __init__(self):
                self.entered = threading.Event()
                self.release = threading.Event()
                self.calls = []
            def decide(self,snapshot,role):
                self.calls.append(snapshot)
                self.entered.set()
                self.release.wait(2)
                key = snapshot['fresh_input_keys'][-1]
                return Decision('none',(),'reply:' + snapshot['captions'][-1]['text'],(key,))
            def close(self):
                self.release.set()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root/'config.yaml'
            config_path.write_text('app:\n  id: synthetic\n  secret: synthetic\nmeeting:\n  meeting_no: "012345678"\n')
            config_path.chmod(0o600)
            description,role = root/'description.md',root/'role.md'
            description.write_text('本地背景')
            role.write_text('根据会议内容回答。')
            backend = Backend()
            def config_factory():
                config = load_config(config_path)
                return replace(config,data_directory=root/'harness',
                    llm=replace(config.llm,backend='codex',role_file=role,introduction=False,
                                batch_seconds=0,reply_interval_seconds=0),
                    meeting_context=replace(config.meeting_context,calendar_id='harness',event_id='harness'))
            records = []
            def display(value,flush=False):
                records.append(json.loads(value))
            with patch('feishu_meeting_adviser.adviser.create_backend',return_value=backend):
                harness = Harness(config_factory,description,root,display=display)
                try:
                    harness.submit('张三','第一句')
                    self.assertTrue(backend.entered.wait(2))
                    harness.submit('李四','模型运行期间的新发言')
                    backend.release.set()
                    deadline = time.monotonic()+4
                    while time.monotonic()<deadline and not any(r.get('kind')=='reply' and r.get('status')=='allowed' for r in records):
                        time.sleep(.05)
                    replies = [r for r in records if r.get('kind')=='reply' and r.get('status')=='allowed']
                    self.assertEqual([r['text'] for r in replies],['reply:模型运行期间的新发言'])
                    self.assertGreaterEqual(len(backend.calls),2)
                finally:
                    harness.close()


if __name__ == '__main__':
    unittest.main()
