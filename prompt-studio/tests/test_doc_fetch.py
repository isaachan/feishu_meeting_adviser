"""Tests for the optional Feishu doc fetch (personal noam/my-lark)."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

from prompt_studio import cli
from prompt_studio.doc_fetch import (DocFetchError, fetch_doc, validate_url)

URL = 'https://nio.feishu.cn/docx/AbCdEf1234567890'


class UrlTests(unittest.TestCase):
    def test_accepts_docx_and_wiki(self):
        self.assertEqual(validate_url(URL), URL)
        wiki = 'https://nio.feishu.cn/wiki/WikiToken123?from=chat#top'
        self.assertEqual(validate_url(wiki), wiki)
        lark = 'https://tenant.larksuite.com/docx/LarkToken123/'
        self.assertEqual(validate_url(lark), lark)

    def test_rejects_unsafe_or_unsupported(self):
        for value in (
            'http://nio.feishu.cn/docx/AbCdEf1234567890',
            'https://evil.example/docx/AbCdEf1234567890',
            'https://nio.feishu.cn/applink/AbCdEf1234567890',
            'https://nio.feishu.cn/docx/',
            'https://user:pass@nio.feishu.cn/docx/AbCdEf1234567890',
            'https://nio.feishu.cn:8443/docx/AbCdEf1234567890',
            'AbCdEf1234567890',
            '',
        ):
            with self.subTest(url=value):
                with self.assertRaises(DocFetchError):
                    validate_url(value)


class FetchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def fake_noam(self, payload, *, code=0, stderr=''):
        log = self.root / 'args.json'
        script = self.root / 'fake-noam'
        script.write_text(
            '#!/usr/bin/env python3\n'
            'import json,os,sys\n'
            f'with open({str(log)!r}, "w") as f:\n'
            '    json.dump({"argv": sys.argv[1:], '
            '"profile": os.environ.get("NOAM_PROFILE")}, f)\n'
            f'if {code}:\n'
            f'    sys.stderr.write({stderr!r})\n'
            f'    sys.exit({code})\n'
            f'print(json.dumps({payload!r}))\n',
            encoding='utf-8')
        script.chmod(script.stat().st_mode | stat.S_IXUSR)
        return script, log

    def test_fetches_nested_markdown_and_passes_args(self):
        script, log = self.fake_noam(
            {'data': {'title': '预算表', 'markdown': '# 预算\n\n500 万'}})
        doc = fetch_doc(URL, noam=script, account='work', profile='dev')
        self.assertEqual(doc.title, '预算表')
        self.assertEqual(doc.text, '# 预算\n\n500 万')
        self.assertEqual(doc.url, URL)
        logged = json.loads(log.read_text(encoding='utf-8'))
        self.assertEqual(logged['profile'], 'dev')
        self.assertEqual(logged['argv'], [
            'my-lark', 'docs', '+fetch', '--doc', URL,
            '--doc-format', 'markdown', '--format', 'json',
            '--account', 'work'])

    def test_block_list_is_joined(self):
        script, _ = self.fake_noam(
            {'data': {'title': '块文档',
                      'blocks': [{'text': '第一段'}, {'text': '第二段'}]}})
        doc = fetch_doc(URL, noam=script)
        self.assertEqual(doc.title, '块文档')
        self.assertEqual(doc.text, '第一段\n\n第二段')

    def test_failure_has_configure_hint(self):
        script, _ = self.fake_noam({}, code=2,
                                   stderr='FeatureDisabled: my-lark disabled')
        with self.assertRaises(DocFetchError) as caught:
            fetch_doc(URL, noam=script)
        self.assertIn('noam features enable my-lark', str(caught.exception))
        self.assertIn('--noam-profile dev', str(caught.exception))

    def test_empty_document_rejected(self):
        script, _ = self.fake_noam({'data': {'title': '空'}})
        with self.assertRaises(DocFetchError) as caught:
            fetch_doc(URL, noam=script)
        self.assertIn('没有可读文本', str(caught.exception))

    def test_cli_appends_to_scene(self):
        script, _ = self.fake_noam(
            {'title': '原始标题', 'content': '正文内容'})
        scene = self.root / 'scene.json'
        scene.write_text(json.dumps({
            'title': '预算会', 'extra': {'keep': True},
            'documents': [],
            'turns': [{'kind': 'human', 'speaker': '本机', 'text': '开始'}],
        }, ensure_ascii=False), encoding='utf-8')
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(['doc', 'fetch', '--url', URL,
                             '--scene', str(scene), '--noam', str(script),
                             '--title', '预算表', '--json'])
        self.assertEqual(code, 0, err.getvalue())
        payload = json.loads(out.getvalue())
        self.assertEqual(payload['title'], '预算表')
        self.assertEqual(payload['scene'], str(scene))
        saved = json.loads(scene.read_text(encoding='utf-8'))
        self.assertEqual(saved['extra'], {'keep': True})
        self.assertEqual(saved['documents'],
                         [{'kind': 'doc', 'title': '预算表', 'text': '正文内容'}])
        self.assertEqual(saved['turns'][0]['text'], '开始')

    def test_cli_writes_text_and_prints_it(self):
        script, _ = self.fake_noam({'title': '预算表', 'markdown': '正文'})
        output = self.root / 'doc.md'
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(['doc', 'fetch', '--url', URL,
                             '--output', str(output), '--noam', str(script)])
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn('wrote:', out.getvalue())
        self.assertEqual(output.read_text(encoding='utf-8'), '正文')
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)

        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(['doc', 'fetch', '--url', URL,
                             '--noam', str(script)])
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(out.getvalue(), '正文\n')


if __name__ == '__main__':
    unittest.main()
