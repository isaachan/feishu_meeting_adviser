"""Fetch a Feishu doc into a scene document via the personal noam CLI.

The personal ``my-lark`` module reads documents through the user's Feishu web
session; it is not an OpenAPI app and the studio never sees the session cookie.
Only a HTTPS docx/wiki URL is accepted, and the URL is passed as a single argv
item (no shell).
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import urlsplit

DOC_PATH = re.compile(r'^/(?:docx|wiki)/(?P<token>[A-Za-z0-9_-]{4,})$')
PROFILE = re.compile(r'^[A-Za-z0-9_-]{1,40}$')
TITLE_KEYS = ('title', 'name')
TEXT_KEYS = ('markdown', 'content', 'text', 'body')
NESTED_KEYS = ('data', 'document', 'doc', 'result', 'blocks')


class DocFetchError(RuntimeError):
    pass


@dataclass(frozen=True)
class FetchedDoc:
    title: str
    text: str
    url: str

    def to_dict(self) -> dict:
        return {'title': self.title, 'text': self.text, 'url': self.url}


def validate_url(url: str) -> str:
    if not isinstance(url, str) or not url.strip():
        raise DocFetchError('--url 不能为空')
    parsed = urlsplit(url.strip())
    if parsed.scheme != 'https':
        raise DocFetchError('只支持 https 飞书文档 URL')
    if parsed.username or parsed.password:
        raise DocFetchError('URL 不能包含用户信息')
    if parsed.port not in (None, 443):
        raise DocFetchError('URL 端口必须是 443')
    host = (parsed.hostname or '').lower()
    if not _feishu_host(host):
        raise DocFetchError('只支持 *.feishu.cn 或 *.larksuite.com 文档 URL')
    match = DOC_PATH.fullmatch(parsed.path.rstrip('/'))
    if match is None:
        raise DocFetchError('URL 必须是 /docx/<token> 或 /wiki/<token>')
    return url.strip()


def _feishu_host(host: str) -> bool:
    return any(host == root or host.endswith('.' + root)
               for root in ('feishu.cn', 'larksuite.com'))


def find_noam(explicit: str | Path | None = None) -> str:
    if explicit is not None:
        candidate = Path(explicit).expanduser()
        if not candidate.is_file():
            raise DocFetchError(f'noam 不存在: {candidate}')
        return str(candidate)
    found = shutil.which('noam')
    if found:
        return found
    fallback = Path.home() / '.local' / 'bin' / 'noam'
    if fallback.is_file():
        return str(fallback)
    raise DocFetchError('找不到 noam CLI；安装 noam 或传 --noam <path>')


def fetch_doc(url: str, *, noam: str | Path | None = None,
              account: str | None = None, profile: str | None = None,
              timeout: float = 90.0,
              runner=subprocess.run) -> FetchedDoc:
    target = validate_url(url)
    executable = find_noam(noam)
    if profile is not None and not PROFILE.fullmatch(profile):
        raise DocFetchError('--noam-profile 含非法字符')
    args = [executable, 'my-lark', 'docs', '+fetch',
            '--doc', target, '--doc-format', 'markdown', '--format', 'json']
    if account:
        args += ['--account', account]
    env = dict(os.environ)
    if profile:
        env['NOAM_PROFILE'] = profile
    try:
        completed = runner(args, capture_output=True, text=True, timeout=timeout,
                           env=env, check=False)
    except FileNotFoundError:
        raise DocFetchError(f'无法执行 noam: {executable}') from None
    except OSError as exc:
        raise DocFetchError(f'无法执行 noam: {executable}: {exc}') from None
    except subprocess.TimeoutExpired:
        raise DocFetchError(f'noam 超时（{timeout:g}s）') from None
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or '').strip()
        raise DocFetchError(_command_error(detail))
    try:
        payload = json.loads(completed.stdout)
    except ValueError:
        raise DocFetchError('noam 返回的不是 JSON；检查 my-lark 会话是否有效') from None
    text = _find_text(payload)
    if not text or not text.strip():
        raise DocFetchError('文档没有可读文本（空文档或不支持的返回格式）')
    title = _find_title(payload) or _fallback_title(target)
    return FetchedDoc(title=title.strip(), text=text.strip(), url=target)


def _command_error(detail: str) -> str:
    message = f'noam 抓取失败: {detail[-1200:]}' if detail else 'noam 抓取失败'
    if 'FeatureDisabled' in detail or 'disabled' in detail:
        message += ('\n提示: `noam features enable my-lark`，'
                    '或使用 `--noam-profile dev`')
    elif 'session is missing' in detail or 'web session' in detail:
        message += ('\n提示: 先运行 `noam my-lark auth qr-login`'
                    '（dev profile 时加 `NOAM_PROFILE=dev`）')
    return message


def _find_text(value) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [part for part in (_find_text(item) for item in value)
                 if part and part.strip()]
        return '\n\n'.join(parts) if parts else None
    if not isinstance(value, dict):
        return None
    for key in TEXT_KEYS:
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            return item
    for key in NESTED_KEYS:
        item = value.get(key)
        if isinstance(item, (dict, list)):
            text = _find_text(item)
            if text:
                return text
    return None


def _find_title(value) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in TITLE_KEYS:
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            return item
    for key in NESTED_KEYS:
        item = value.get(key)
        if isinstance(item, (dict, list)):
            title = _find_title(item)
            if title:
                return title
    return None


def _fallback_title(url: str) -> str:
    match = DOC_PATH.fullmatch(urlsplit(url).path.rstrip('/'))
    token = match.group('token') if match else 'unknown'
    return f'飞书文档 {token[:12]}'


def append_scene_document(scene_path: str | Path, title: str, text: str) -> Path:
    target = Path(scene_path)
    try:
        raw = json.loads(target.read_text(encoding='utf-8'))
    except OSError as exc:
        raise DocFetchError(f'读取场景失败: {exc}') from None
    except ValueError as exc:
        raise DocFetchError(f'场景不是合法 JSON: {exc}') from None
    if not isinstance(raw, dict):
        raise DocFetchError('场景必须是 JSON 对象')
    documents = raw.get('documents')
    if documents is None:
        documents = []
        raw['documents'] = documents
    if not isinstance(documents, list) or any(
            not isinstance(item, dict) for item in documents):
        raise DocFetchError('场景 documents 必须是对象数组')
    documents.append({'kind': 'doc', 'title': title, 'text': text})
    _write_atomic(target, json.dumps(raw, ensure_ascii=False, indent=2) + '\n')
    return target


def write_text(path: str | Path, text: str) -> Path:
    target = Path(path)
    _write_atomic(target, text)
    return target


def _write_atomic(target: Path, content: str):
    parent = target.parent if str(target.parent) else Path('.')
    try:
        parent.mkdir(parents=True, exist_ok=True)
        temp = parent / f'.{target.name}.{os.getpid()}.tmp'
        with open(temp, 'w', encoding='utf-8') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, target)
    except OSError as exc:
        raise DocFetchError(f'写入失败: {target}: {exc}') from None
    finally:
        try:
            if 'temp' in locals() and temp.exists():
                temp.unlink()
        except OSError:
            pass

