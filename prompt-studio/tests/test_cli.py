import io
import json
import os
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from prompt_studio import cli
from prompt_studio.stack import PROMPT_FILES

SAMPLE = """会参谋 · 文字记录
会话 s-old · 会议 111111111 · 预算会
导出时间 2026/11/26 16:00:00 · 共 3 条

[2026/11/26 16:02:03] 本机: 我们过一下预算
[时间未知] LLM: 历史回复
[2026/11/26 16:02:10] 未识别: 我补充一点
"""


class InitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data_dir = Path(self.temp.name) / 'data'

    def run_init(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(['init', '--data-dir', str(self.data_dir), *args])
        return code, out.getvalue(), err.getvalue()

    def test_init_copies_prompts(self):
        code, out, _ = self.run_init()
        self.assertEqual(code, 0)
        self.assertIn(str(self.data_dir / 'prompts'), out)
        source = cli._gateway_prompts()
        for name in PROMPT_FILES:
            self.assertEqual((self.data_dir / 'prompts' / name).read_text(),
                             (source / name).read_text())

    def test_init_keeps_edits_unless_forced(self):
        self.assertEqual(self.run_init()[0], 0)
        target = self.data_dir / 'prompts' / PROMPT_FILES[0]
        target.write_text('edited', encoding='utf-8')
        code, out, _ = self.run_init()
        self.assertEqual(code, 0)
        self.assertIn('kept:', out)
        self.assertEqual(target.read_text(), 'edited')
        code, _, _ = self.run_init('--force')
        self.assertEqual(code, 0)
        source = cli._gateway_prompts()
        self.assertEqual(target.read_text(), (source / PROMPT_FILES[0]).read_text())


class FakeStack:
    instances = []

    def __init__(self, config):
        self.config = config
        self.started = None
        self.submitted = None
        self.stopped = False
        FakeStack.instances.append(self)

    def start(self, *, auth_only=False):
        self.started = auth_only
        return self

    def ensure_owner(self, owner, **kwargs):
        self.submitted = (owner, kwargs)

    def stop(self):
        self.stopped = True


class SubmitTokenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data_dir = Path(self.temp.name) / 'data'
        FakeStack.instances = []

    def test_requires_a_token(self):
        env = {'NSDP_TOKEN': '', 'JIRA_TOKEN': ''}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(cli, 'Stack', FakeStack):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main(['submit-tokens', '--data-dir', str(self.data_dir)])
        self.assertEqual(code, 1)
        self.assertIn('没有 token', err.getvalue())
        self.assertEqual(FakeStack.instances, [])

    def test_submits_from_files_without_echo(self):
        token_file = Path(self.temp.name) / 'nsdp.txt'
        token_file.write_text('secret-nsdp-token\n', encoding='utf-8')
        env = {'NSDP_TOKEN': '', 'JIRA_TOKEN': '',
               'PROMPT_STUDIO_OWNER': 'ou_test'}
        with mock.patch.dict(os.environ, env), \
                mock.patch.object(cli, 'Stack', FakeStack):
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main(['submit-tokens', '--data-dir', str(self.data_dir),
                                 '--nsdp-token-file', str(token_file)])
        self.assertEqual(code, 0)
        self.assertEqual(len(FakeStack.instances), 1)
        stack = FakeStack.instances[0]
        self.assertTrue(stack.started)
        self.assertTrue(stack.stopped)
        self.assertEqual(stack.submitted[0], 'ou_test')
        self.assertEqual(stack.submitted[1]['nsdp_token'], 'secret-nsdp-token')
        self.assertNotIn('secret-nsdp-token', out.getvalue())


class ReplayArgumentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data_dir = Path(self.temp.name) / 'data'
        self.transcript = Path(self.temp.name) / 'meeting.txt'
        self.transcript.write_text(SAMPLE, encoding='utf-8')

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_requires_source(self):
        with self.assertRaises(SystemExit) as caught, \
                redirect_stderr(io.StringIO()):
            cli.main(['replay'])
        self.assertEqual(caught.exception.code, 2)

    def test_backend_option_removed(self):
        with self.assertRaises(SystemExit) as caught, \
                redirect_stderr(io.StringIO()):
            cli.main(['replay', '--transcript', str(self.transcript),
                      '--backend', 'fake'])
        self.assertEqual(caught.exception.code, 2)

    def test_step_mode_removed(self):
        with self.assertRaises(SystemExit) as caught, \
                redirect_stderr(io.StringIO()):
            cli.main(['replay', '--transcript', str(self.transcript),
                      '--mode', 'step'])
        self.assertEqual(caught.exception.code, 2)

    def test_rejects_bad_paced_seconds(self):
        code, _, err = self.run_cli(
            'replay', '--transcript', str(self.transcript),
            '--mode', 'paced', '--paced-seconds', '-1')
        self.assertEqual(code, 1)
        self.assertIn('paced seconds', err)

    def test_rejects_both_sources(self):
        with self.assertRaises(SystemExit) as caught, \
                redirect_stderr(io.StringIO()):
            cli.main(['replay', '--transcript', 'a.txt', '--scene', 'b.json'])
        self.assertEqual(caught.exception.code, 2)

    def test_missing_transcript(self):
        code, _, err = self.run_cli('replay', '--transcript',
                                    str(Path(self.temp.name) / 'nope.txt'))
        self.assertEqual(code, 1)
        self.assertIn('读取场景失败', err)

    def test_invalid_meeting_no(self):
        code, _, err = self.run_cli(
            'replay', '--transcript', str(self.transcript),
            '--meeting-no', '123', '--data-dir', str(self.data_dir))
        self.assertEqual(code, 1)
        self.assertIn('meeting-no', err)

    def test_invalid_run_id(self):
        code, _, err = self.run_cli(
            'replay', '--transcript', str(self.transcript),
            '--run-id', '../escape', '--data-dir', str(self.data_dir))
        self.assertEqual(code, 1)
        self.assertIn('run-id', err)

    def test_scene_description_and_document_override(self):
        scene = Path(self.temp.name) / 'scene.json'
        from prompt_studio.transcript import parse_text
        scene.write_text(json.dumps(parse_text(SAMPLE).to_dict(), ensure_ascii=False),
                         encoding='utf-8')
        document = Path(self.temp.name) / '预算表.txt'
        document.write_text('总预算 500 万', encoding='utf-8')
        args = mock.Mock(scene=scene, transcript=None, title='新标题',
                         description='新描述', document=[document])
        transcript = cli._load_transcript(args)
        self.assertEqual(transcript.title, '新标题')
        self.assertEqual(transcript.description, '新描述')
        self.assertEqual(transcript.documents[0]['title'], '预算表')
        self.assertEqual(transcript.documents[0]['text'], '总预算 500 万')

    def test_transcript_from_stdin(self):
        args = mock.Mock(scene=None, transcript=Path('-'), title=None,
                         description=None, document=[])
        with mock.patch('sys.stdin', io.StringIO(SAMPLE)):
            transcript = cli._load_transcript(args)
        self.assertEqual(len(transcript.human_turns), 2)


class ServeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data_dir = Path(self.temp.name) / 'data'

    def test_rejects_unknown_host(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(['serve', '--host', '10.0.0.2',
                             '--data-dir', str(self.data_dir)])
        self.assertEqual(code, 1)
        self.assertIn('0.0.0.0', err.getvalue())

    def test_rejects_bad_port(self):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(['serve', '--port', '70000',
                             '--data-dir', str(self.data_dir)])
        self.assertEqual(code, 1)
        self.assertIn('port', err.getvalue())

    def test_default_prompt_dir_is_seeded(self):
        prompt_dir = cli._ensure_web_prompts(self.data_dir, None)
        source = cli._gateway_prompts()
        self.assertEqual(prompt_dir, self.data_dir / 'prompts')
        for name in PROMPT_FILES:
            self.assertEqual((prompt_dir / name).read_text(encoding='utf-8'),
                             (source / name).read_text(encoding='utf-8'))

    def test_explicit_prompt_dir_must_be_complete(self):
        prompt_dir = Path(self.temp.name) / 'prompts'
        prompt_dir.mkdir()
        with self.assertRaises(cli.CommandError):
            cli._ensure_web_prompts(self.data_dir, prompt_dir)


if __name__ == '__main__':
    unittest.main()
