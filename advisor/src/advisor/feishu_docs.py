"""Feishu document background material referenced by calendar events.

The calendar event description is already stored as session material; this
module extends it with the plain text of Feishu docx/wiki links found in that
description. Fetching is best-effort: failures never block session creation,
the caller degrades to the description alone (and may ask the organizer to
re-authorize when the OAuth credential lacks a document scope).

Only official API endpoints built from a strictly validated token are called;
never fetch arbitrary URLs from the description (SSRF guard).
"""
import json
import logging
import re
import time
from dataclasses import dataclass, field
from urllib import error, parse, request

from .auth_client import AuthUnavailable, AuthorizationRequired

LOG = logging.getLogger(__name__)

BASE_URL = 'https://open.feishu.cn'
DOCX_RAW_PATH = '/open-apis/docx/v1/documents/{token}/raw_content'
WIKI_NODE_PATH = '/open-apis/wiki/v2/spaces/get_node'

MAX_DOCUMENTS = 10
MAX_DOC_CHARACTERS = 20_000
MAX_TOTAL_CHARACTERS = 60_000
FETCH_BUDGET_S = 3.0
REQUEST_TIMEOUT_S = 2.0
MAX_RESPONSE_BYTES = 1 << 20

TOKEN_INVALID_CODES = frozenset({20005, 99991661, 99991663, 99991668, 99991677})
DOCX_SCOPES = frozenset({'docx:document:readonly', 'docx:document'})
WIKI_SCOPES = frozenset({'wiki:wiki:readonly', 'wiki:wiki'})

DOC_LINK = re.compile(
    r'https?://(?:[A-Za-z0-9-]+\.)*(?:feishu\.cn|larksuite\.com)/'
    r'(?P<kind>docx|wiki)/(?P<token>[A-Za-z0-9_-]{4,128})',
    re.IGNORECASE)
DOC_TOKEN = re.compile(r'[A-Za-z0-9_-]{4,128}\Z')


@dataclass
class DocsFetch:
    documents: list[dict] = field(default_factory=list)
    partial: bool = False
    reauthorize: bool = False


def extract_doc_refs(description: str) -> list[tuple[str, str, str]]:
    """(kind, token, url) for docx/wiki links, in first-seen order."""
    refs: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for match in DOC_LINK.finditer(description or ''):
        kind = match.group('kind').lower()
        token = match.group('token')
        if not DOC_TOKEN.fullmatch(token):
            continue
        key = (kind, token)
        if key in seen:
            continue
        seen.add(key)
        refs.append((kind, token, match.group(0)))
    return refs


class _TokenRejected(Exception):
    pass


class _Unavailable(Exception):
    def __init__(self, code=None):
        self.code = code
        super().__init__(code)


class FeishuDocsFetcher:
    """Fetch docx/wiki text with the organizer's user token (best-effort)."""

    def __init__(self, auth, *, base_url=BASE_URL, timeout=REQUEST_TIMEOUT_S,
                 budget_s=FETCH_BUDGET_S, clock=time.monotonic, opener=None):
        self.auth = auth
        self.base_url = base_url.rstrip('/')
        self.timeout = timeout
        self.budget_s = budget_s
        self._clock = clock
        self._open = opener or request.build_opener(request.ProxyHandler({})).open

    def fetch(self, owner_open_id, description: str) -> DocsFetch:
        refs = extract_doc_refs(description)[:MAX_DOCUMENTS]
        if not refs:
            return DocsFetch()
        try:
            credential = self.auth.get_token(owner_open_id)
        except (AuthUnavailable, AuthorizationRequired):
            return DocsFetch(partial=True)
        scopes = set(credential.get('scopes') or ())
        token = credential.get('access_token')
        if not isinstance(token, str) or not token:
            return DocsFetch(partial=True)

        result = DocsFetch()
        allowed: list[tuple[str, str, str]] = []
        for kind, doc_token, url in refs:
            needed = DOCX_SCOPES if kind == 'docx' else WIKI_SCOPES
            if scopes.isdisjoint(needed):
                result.reauthorize = True
                result.partial = True
                continue
            allowed.append((kind, doc_token, url))

        deadline = self._clock() + self.budget_s
        total = 0
        for kind, doc_token, url in allowed:
            if self._clock() >= deadline:
                result.partial = True
                break
            try:
                title, text = self._load(kind, doc_token, token, deadline)
            except _TokenRejected:
                try:
                    token = self.auth.get_token(
                        owner_open_id, rejected_token=token)['access_token']
                except (AuthUnavailable, AuthorizationRequired, KeyError):
                    result.partial = True
                    break
                try:
                    title, text = self._load(kind, doc_token, token, deadline)
                except (_TokenRejected, _Unavailable):
                    result.partial = True
                    continue
            except _Unavailable as exc:
                LOG.info('feishu document unavailable: kind=%s code=%s',
                         kind, exc.code)
                result.partial = True
                continue
            text = text or ''
            if not text.strip():
                result.partial = True
                continue
            limit = min(MAX_DOC_CHARACTERS, MAX_TOTAL_CHARACTERS - total)
            if limit <= 0:
                result.partial = True
                break
            truncated = len(text) > limit
            result.documents.append({
                'kind': kind, 'title': title, 'url': url,
                'text': text[:limit], 'truncated': truncated})
            total += min(len(text), limit)
            if truncated:
                result.partial = True
            if total >= MAX_TOTAL_CHARACTERS:
                break
        return result

    def _load(self, kind, doc_token, token, deadline):
        if kind == 'wiki':
            data = self._get(WIKI_NODE_PATH, {'token': doc_token,
                                              'obj_type': 'wiki'}, token, deadline)
            node = data.get('node')
            if not isinstance(node, dict):
                raise _Unavailable('invalid_wiki_node')
            obj_type = node.get('obj_type')
            obj_token = node.get('obj_token')
            if obj_type != 'docx' or not isinstance(obj_token, str) \
                    or not DOC_TOKEN.fullmatch(obj_token):
                raise _Unavailable('unsupported_wiki_object')
            title = node.get('title') if isinstance(node.get('title'), str) else ''
            return title, self._raw_content(obj_token, token, deadline)
        return '', self._raw_content(doc_token, token, deadline)

    def _raw_content(self, doc_token, token, deadline):
        data = self._get(DOCX_RAW_PATH.format(
            token=parse.quote(doc_token, safe='')), None, token, deadline)
        content = data.get('content')
        if not isinstance(content, str):
            raise _Unavailable('invalid_content')
        return content

    def _get(self, path, query, token, deadline):
        url = self.base_url + path
        if query:
            url += '?' + parse.urlencode(query)
        remaining = deadline - self._clock()
        req = request.Request(url, method='GET', headers={
            'Authorization': 'Bearer ' + token, 'Accept': 'application/json'})
        try:
            with self._open(req, timeout=max(0.1, min(self.timeout, remaining))) as response:
                status = getattr(response, 'status', getattr(response, 'code', 200))
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except error.HTTPError as exc:
            try:
                raw = exc.read(MAX_RESPONSE_BYTES + 1)
            except OSError:
                raw = b''
            status = exc.code
        except (error.URLError, TimeoutError, OSError):
            raise _Unavailable('transport') from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise _Unavailable('too_large')
        try:
            result = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise _Unavailable('invalid_json') from None
        if not isinstance(result, dict):
            raise _Unavailable('invalid_json')
        code = result.get('code')
        if 200 <= status < 300 and code == 0:
            data = result.get('data')
            return data if isinstance(data, dict) else {}
        if isinstance(code, int) and code in TOKEN_INVALID_CODES:
            raise _TokenRejected()
        raise _Unavailable(code)
