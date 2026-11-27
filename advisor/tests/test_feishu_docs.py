"""Feishu document material extraction and fetch tests (injected HTTP)."""
import io
import json
import unittest
from urllib import error

from advisor.feishu_docs import (DOCX_RAW_PATH, WIKI_NODE_PATH, FeishuDocsFetcher,
                                 extract_doc_refs)

DOCX_A = 'doxcnAAAAAAAAAAAA'
DOCX_B = 'doxcnBBBBBBBBBBBB'
WIKI_A = 'wikcnCCCCCCCCCCCC'
DOCX_SCOPES = ('docx:document:readonly', 'wiki:wiki:readonly')


class FakeAuth:
    def __init__(self, scopes=DOCX_SCOPES):
        self.scopes = list(scopes)
        self.calls = []

    def get_token(self, open_id, *, rejected_token=None):
        self.calls.append((open_id, rejected_token))
        return {'access_token': 'token-2' if rejected_token else 'token-1',
                'scopes': self.scopes}


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload, self.status = payload, status

    def read(self, _limit=None):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class Rejected:
    def __init__(self, code, status=403):
        self.code, self.status = code, status


class FakeHTTP:
    def __init__(self, pages, *, token_invalid_once=False):
        self.pages = pages
        self.token_invalid_once = token_invalid_once
        self.calls = []

    def __call__(self, req, timeout=None):
        path = req.selector.split('?')[0]
        self.calls.append(path)
        if self.token_invalid_once:
            self.token_invalid_once = False
            raise error.HTTPError(req.full_url, 400, 'bad', {},
                                  io.BytesIO(json.dumps({'code': 99991661}).encode()))
        payload = self.pages.get(path)
        if payload is None:
            return FakeResponse({'code': 99991672, 'msg': 'denied'}, status=403)
        if callable(payload):
            payload = payload(req)
        if isinstance(payload, Rejected):
            return FakeResponse({'code': payload.code, 'msg': 'rejected'},
                                status=payload.status)
        return FakeResponse({'code': 0, 'data': payload})


def docx_path(token):
    return DOCX_RAW_PATH.format(token=token)


class ExtractTests(unittest.TestCase):
    def test_extracts_docx_and_wiki_once_in_order(self):
        description = (
            f'参考 https://nio.feishu.cn/docx/{DOCX_A}?from=calendar 和 '
            f'https://nio.feishu.cn/wiki/{WIKI_A}。重复 '
            f'https://nio.feishu.cn/docx/{DOCX_A}，忽略 '
            'https://nio.feishu.cn/sheets/xxxxyyyyzzzz')
        self.assertEqual(extract_doc_refs(description), [
            ('docx', DOCX_A, f'https://nio.feishu.cn/docx/{DOCX_A}'),
            ('wiki', WIKI_A, f'https://nio.feishu.cn/wiki/{WIKI_A}')])

    def test_ignores_non_feishu_and_short_tokens(self):
        self.assertEqual(extract_doc_refs(
            'https://example.com/docx/abcdefgh 和 https://x.feishu.cn/docx/abc'), [])


class FetchTests(unittest.TestCase):
    def make(self, pages, *, scopes=DOCX_SCOPES, token_invalid_once=False):
        auth = FakeAuth(scopes)
        http = FakeHTTP(pages, token_invalid_once=token_invalid_once)
        fetcher = FeishuDocsFetcher(auth, opener=http, clock=lambda: 0.0)
        return fetcher, auth, http

    def test_fetches_docx_and_wiki_text(self):
        pages = {
            docx_path(DOCX_A): {'content': '第一份文档正文'},
            WIKI_NODE_PATH: {'node': {'obj_type': 'docx', 'obj_token': DOCX_B,
                                      'title': 'WIKI 标题'}},
            docx_path(DOCX_B): {'content': 'wiki 正文'},
        }
        fetcher, _auth, _http = self.make(pages)
        result = fetcher.fetch('ou_A', f'https://nio.feishu.cn/docx/{DOCX_A} '
                                       f'https://nio.feishu.cn/wiki/{WIKI_A}')
        self.assertFalse(result.partial)
        self.assertFalse(result.reauthorize)
        self.assertEqual([(d['kind'], d['title'], d['text']) for d in result.documents], [
            ('docx', '', '第一份文档正文'),
            ('wiki', 'WIKI 标题', 'wiki 正文')])

    def test_missing_scope_degrades_with_reauthorize(self):
        fetcher, _auth, http = self.make({}, scopes=())
        result = fetcher.fetch('ou_A', f'https://nio.feishu.cn/docx/{DOCX_A}')
        self.assertEqual(result.documents, [])
        self.assertTrue(result.partial)
        self.assertTrue(result.reauthorize)
        self.assertEqual(http.calls, [])

    def test_one_rejected_document_keeps_the_others(self):
        pages = {
            docx_path(DOCX_A): {'content': '保留'},
            docx_path(DOCX_B): Rejected(99991672),
        }
        fetcher, _auth, _http = self.make(pages)
        result = fetcher.fetch('ou_A', f'https://nio.feishu.cn/docx/{DOCX_A} '
                                       f'https://nio.feishu.cn/docx/{DOCX_B}')
        self.assertEqual([d['text'] for d in result.documents], ['保留'])
        self.assertTrue(result.partial)
        self.assertFalse(result.reauthorize)

    def test_token_rejected_refreshes_once(self):
        pages = {docx_path(DOCX_A): {'content': '正文'}}
        fetcher, auth, _http = self.make(pages, token_invalid_once=True)
        result = fetcher.fetch('ou_A', f'https://nio.feishu.cn/docx/{DOCX_A}')
        self.assertEqual([d['text'] for d in result.documents], ['正文'])
        self.assertEqual(auth.calls, [('ou_A', None), ('ou_A', 'token-1')])

    def test_truncates_oversized_document(self):
        pages = {docx_path(DOCX_A): {'content': 'x' * 20_001}}
        fetcher, _auth, _http = self.make(pages)
        result = fetcher.fetch('ou_A', f'https://nio.feishu.cn/docx/{DOCX_A}')
        document = result.documents[0]
        self.assertEqual(len(document['text']), 20_000)
        self.assertTrue(document['truncated'])
        self.assertTrue(result.partial)

    def test_unsupported_wiki_object_is_skipped(self):
        pages = {WIKI_NODE_PATH: {'node': {'obj_type': 'sheet', 'obj_token': DOCX_B,
                                           'title': '表格'}}}
        fetcher, _auth, _http = self.make(pages)
        result = fetcher.fetch('ou_A', f'https://nio.feishu.cn/wiki/{WIKI_A}')
        self.assertEqual(result.documents, [])
        self.assertTrue(result.partial)


if __name__ == '__main__':
    unittest.main()
