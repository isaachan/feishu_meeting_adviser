"""Read-only feasibility probe; explicit document checks do not prove event linkage.

Run with ``python -m feishu_meeting_adviser.context_probe --help``.
Only explicit calendar, Docx and Wiki reads are available; user OAuth is separate.
No source/session database, bot action, model call, or raw response export.
"""
import argparse
from datetime import datetime, timezone
import html
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import sys
from urllib import request
from urllib.parse import quote, urlsplit
from uuid import uuid4

from .client import ApiError, ErrorKind, FeishuClient, StopRequested, TOKEN
from .config import ConfigError, DEFAULT_CONFIG, load_config
from .oauth import OAuthError, OAuthManager, _NoRedirect
from .storage import StorageError, private_directory


PREVIEW_CHARACTERS = 2000
MAX_DOCUMENTS = 10
MAX_ATTACHMENTS = 30
MAX_REPORT_BYTES = 250_000
_ID = re.compile(r'[A-Za-z0-9_.@-]{1,256}\Z')
_READ_PATH = re.compile(
    r'/open-apis/(?:calendar/v4/calendars(?:/[A-Za-z0-9_.%~-]+/events(?:/[A-Za-z0-9_.%~-]+)?)?'
    r'|docx/v1/documents/[A-Za-z0-9_.%~-]+(?:/raw_content)?|wiki/v2/spaces/get_node)\Z')
_URL = re.compile(r'https://[^\s<>\"\']+')


class ProbeInputError(ValueError):
    pass


def resource_id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value) or value in ('.', '..'):
        raise ProbeInputError('invalid resource identifier; value hidden')
    return value


def document_reference(url):
    """Normalize only direct Feishu Docx/Wiki links; never fetch the supplied host."""
    if not isinstance(url, str) or len(url) > 4096:
        raise ProbeInputError('invalid document URL; value hidden')
    try:
        parsed = urlsplit(html.unescape(url))
        host = parsed.hostname or ''
        valid_host = host == 'feishu.cn' or host.endswith('.feishu.cn')
        match = re.fullmatch(r'/(docx|wiki)/([A-Za-z0-9]+)(?:/)?', parsed.path)
        if (parsed.scheme != 'https' or not valid_host or parsed.username or parsed.password
                or parsed.port is not None or not match):
            raise ValueError()
    except ValueError:
        raise ProbeInputError('expected a direct HTTPS Feishu Docx or Wiki URL; value hidden') from None
    kind, token = match.groups()
    resource_id(token)
    return {'type': kind, 'token': token, 'source': f'https://{host}/{kind}/{token}'}


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            self.links.extend(value for key, value in attrs if key == 'href' and value)


def description_references(description):
    parser = _Links()
    parser.feed(description)
    references, seen = [], set()
    for candidate in parser.links + _URL.findall(html.unescape(description)):
        try:
            reference = document_reference(candidate.rstrip('.,;，。；)）'))
        except ProbeInputError:
            continue
        key = (reference['type'], reference['token'])
        if key not in seen:
            references.append(reference)
            seen.add(key)
    return references


def preview(text):
    if not isinstance(text, str):
        raise ApiError(ErrorKind.RESPONSE)
    return {'characters': len(text), 'preview': text[:PREVIEW_CHARACTERS],
            'truncated': len(text) > PREVIEW_CHARACTERS}


def _data(result, key=None):
    data = result.get('data')
    if not isinstance(data, dict):
        raise ApiError(ErrorKind.RESPONSE)
    value = data.get(key) if key else data
    if not isinstance(value, dict):
        raise ApiError(ErrorKind.RESPONSE)
    return value


def _failure(exc):
    return {'status': 'failed', 'error_kind': exc.kind.value,
            'http_status': exc.http_status, 'code': exc.code}


class ContextProbeClient(FeishuClient):
    identity = 'application'

    def _validate_request(self, path, *, payload=None):
        if path == TOKEN:
            if payload is None:
                raise ValueError('token acquisition requires application credentials')
        elif payload is not None or not _READ_PATH.fullmatch(path):
            raise ValueError('probe permits only allowlisted read endpoints')

    def authenticated(self, path, *, payload=None, query=None, timeout=None):
        self._validate_request(path, payload=payload)
        return super().authenticated(path, payload=payload, query=query, timeout=timeout)

    def event(self, calendar_id, event_id, *, timeout=None):
        calendar_id, event_id = resource_id(calendar_id), resource_id(event_id)
        path = f'/open-apis/calendar/v4/calendars/{quote(calendar_id, safe="")}/events/{quote(event_id, safe="")}'
        return _data(self.authenticated(path, timeout=timeout), 'event')

    def node(self, token, *, timeout=None):
        return _data(self.authenticated('/open-apis/wiki/v2/spaces/get_node',
            query={'token': resource_id(token)}, timeout=timeout), 'node')

    def document(self, token, *, timeout=None):
        token = quote(resource_id(token), safe='')
        return _data(self.authenticated(
            f'/open-apis/docx/v1/documents/{token}', timeout=timeout), 'document')

    def document_text(self, token, *, timeout=None):
        token = quote(resource_id(token), safe='')
        value = _data(self.authenticated(
            f'/open-apis/docx/v1/documents/{token}/raw_content', timeout=timeout)).get('content')
        if not isinstance(value, str):
            raise ApiError(ErrorKind.RESPONSE)
        return value

    def calendars(self, page_token=None):
        query = {'page_size': 50}
        if page_token:
            if not isinstance(page_token, str) or len(page_token) > 4096:
                raise ProbeInputError('invalid calendar page token; value hidden')
            query['page_token'] = page_token
        data = _data(self.authenticated('/open-apis/calendar/v4/calendars', query=query))
        items = data.get('calendar_list')
        if (not isinstance(items, list) or len(items) > 50 or any(not isinstance(item, dict) for item in items)
                or type(data.get('has_more')) is not bool):
            raise ApiError(ErrorKind.RESPONSE)
        return {'identity': self.identity, 'calendars': [{'calendar_id': resource_id(item.get('calendar_id')),
            'title': preview(item.get('summary', '')), 'role': item.get('role'), 'type': item.get('type')}
            for item in items[:50]], 'has_more': data.get('has_more'),
            'page_token': data.get('page_token') if isinstance(data.get('page_token'), str) else None}

    def find_event(self, calendar_id, start, end, meeting_no, *, timeout=None):
        resource_id(calendar_id)
        if not isinstance(meeting_no, str) or not re.fullmatch(r'[0-9]{9}', meeting_no):
            raise ProbeInputError('provide a nine-digit meeting number')
        try:
            start_time, end_time = datetime.fromisoformat(start), datetime.fromisoformat(end)
            if start_time.utcoffset() is None or end_time.utcoffset() is None:
                raise ValueError()
            start_seconds, end_seconds = start_time.timestamp(), end_time.timestamp()
            if not 0 < end_seconds - start_seconds <= 86400:
                raise ValueError()
        except (TypeError, ValueError, OverflowError):
            raise ProbeInputError('provide timezone-aware start/end times within one day') from None
        path = f'/open-apis/calendar/v4/calendars/{quote(calendar_id, safe="")}/events'
        data = _data(self.authenticated(path, query={'start_time': int(start_seconds),
            'end_time': int(end_seconds), 'page_size': 1000}, timeout=timeout))
        items = data.get('items')
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ApiError(ErrorKind.RESPONSE)
        matches = []
        for item in items:
            vchat = item.get('vchat', {})
            url = vchat.get('meeting_url', '') if isinstance(vchat, dict) else ''
            if not isinstance(url, str):
                continue
            try:
                parsed = urlsplit(url)
            except ValueError:
                continue
            if parsed.scheme == 'https' and parsed.hostname == 'vc.feishu.cn' and parsed.path.rstrip('/') == '/j/' + meeting_no:
                matches.append({'event_id': resource_id(item.get('event_id')),
                    'title': preview(item.get('summary', '')),
                    'recurring': bool(item.get('recurrence')),
                    'start_time': item.get('start_time'), 'end_time': item.get('end_time')})
        return {'identity': self.identity, 'calendar_id': calendar_id, 'matches': matches,
            'window_event_count': len(items),
            'truncated_possible': len(items) >= 1000 or data.get('has_more') is True,
            'binding_saved': False}


class UserContextClient(ContextProbeClient):
    identity = 'user'

    def __init__(self, credentials, *, oauth_manager, **kwargs):
        self.oauth_manager = oauth_manager
        kwargs.setdefault('opener', request.build_opener(_NoRedirect()).open)
        super().__init__(credentials, **kwargs)

    def _validate_request(self, path, *, payload=None):
        if path == TOKEN or payload is not None:
            raise ValueError('user material client does not acquire application tokens or mutate data')
        super()._validate_request(path, payload=payload)

    def authenticated(self, path, *, payload=None, query=None, timeout=None):
        self._validate_request(path, payload=payload)
        token = self.oauth_manager.access_token(timeout=timeout)
        try:
            return self._request(path, query=query, token=token, timeout=timeout)
        except ApiError as exc:
            # These are token-invalid/expired codes, not missing scopes or document ACL errors.
            if exc.kind != ErrorKind.TOKEN and exc.code not in (99991668, 99991677, 20005):
                raise
        token = self.oauth_manager.access_token(rejected_token=token, timeout=timeout)
        return self._request(path, query=query, token=token, timeout=timeout)


def probe_document(client, reference):
    result = {key: reference[key] for key in ('type', 'source')}
    token = reference['token']
    if reference['type'] == 'wiki':
        try:
            node = client.node(token)
            object_type = node.get('obj_type')
            if not isinstance(object_type, str) or not isinstance(node.get('obj_token'), str):
                raise ApiError(ErrorKind.RESPONSE)
            result['wiki_resolution'] = {'status': 'ok', 'object_type': object_type,
                                         'title': preview(node.get('title', ''))}
            if object_type != 'docx':
                return result | {'status': 'unsupported', 'reason': 'wiki_object_is_not_docx'}
            token = resource_id(node['obj_token'])
        except (ApiError, ProbeInputError) as exc:
            if isinstance(exc, ProbeInputError):
                exc = ApiError(ErrorKind.RESPONSE)
            return result | _failure(exc) | {'stage': 'wiki_resolution'}
    # Metadata is independent of the raw_content response; no atomic remote snapshot is claimed.
    try:
        document = client.document(token)
        result['metadata'] = {'status': 'ok', 'title': preview(document.get('title', '')),
                              'revision_id': document.get('revision_id') if type(document.get('revision_id')) is int else None}
    except ApiError as exc:
        result['metadata'] = _failure(exc)
    try:
        text = client.document_text(token)
        result['body'] = {'status': 'ok', **preview(text)}
        result['status'] = 'ok' if result['metadata']['status'] == 'ok' else 'partial'
    except ApiError as exc:
        result.update(_failure(exc), stage='document_body')
    return result


def probe(client, *, calendar_id=None, event_id=None, document_urls=(), meeting_no=None):
    if bool(calendar_id) != bool(event_id):
        raise ProbeInputError('calendar_id and event_id must be supplied together')
    if not calendar_id and not document_urls:
        raise ProbeInputError('provide a calendar/event binding or at least one document URL')
    if meeting_no is not None and not re.fullmatch(r'[0-9]{9}', meeting_no):
        raise ProbeInputError('meeting number must contain nine ASCII digits')
    expected = [document_reference(url) for url in document_urls]
    expected_keys = {(item['type'], item['token']) for item in expected}
    if len(expected_keys) > MAX_DOCUMENTS:
        raise ProbeInputError('at most ten explicit document references may be checked')
    if calendar_id:
        resource_id(calendar_id)
        resource_id(event_id)
    report = {'identity': getattr(client, 'identity', 'application'), 'read_at': datetime.now(timezone.utc).isoformat(),
              'event': {'status': 'not_requested'}, 'event_linkage': 'not_verified', 'documents': []}
    references = []
    event_keys = set()
    if calendar_id:
        try:
            event = client.event(calendar_id, event_id)
            if event.get('event_id') != event_id:
                raise ApiError(ErrorKind.RESPONSE)
            description = event.get('description', '')
            description_preview = {'status': 'ok' if 'description' in event else 'field_absent', **preview(description)}
            rich_description = event.get('description_rich', '')
            rich_preview = {'status': 'ok' if 'description_rich' in event else 'field_absent', **preview(rich_description)}
            attachments = event.get('attachments', [])
            if not isinstance(attachments, list) or any(not isinstance(item, dict) for item in attachments):
                raise ApiError(ErrorKind.RESPONSE)
            reference_fields = {}
            references = []
            for field, content in (('description', description), ('description_rich', rich_description)):
                for reference in description_references(content):
                    key = (reference['type'], reference['token'])
                    if key not in reference_fields:
                        references.append(reference)
                        reference_fields[key] = []
                    reference_fields[key].append(field)
            event_keys = {(item['type'], item['token']) for item in references}
            listed = []
            for attachment in attachments[:MAX_ATTACHMENTS]:
                token = attachment.get('file_token')
                listed.append({'name': preview(attachment.get('name', '')),
                    'file_token': token if isinstance(token, str) and _ID.fullmatch(token) else None,
                    'file_size': attachment.get('file_size') if isinstance(attachment.get('file_size'), (str, int)) else None,
                    'status': 'unresolved_calendar_attachment',
                    'reason': 'calendar_file_token_is_not_a_document_identifier'})
            vchat = event.get('vchat', {})
            if not isinstance(vchat, dict):
                raise ApiError(ErrorKind.RESPONSE)
            meeting_url = vchat.get('meeting_url', '')
            if not isinstance(meeting_url, str):
                raise ApiError(ErrorKind.RESPONSE)
            # A nine-digit /j/ link is useful evidence, not a unique recurring-event identity.
            meeting_match = 'not_checked'
            if meeting_no:
                parsed = urlsplit(meeting_url)
                match = re.fullmatch(r'/j/([0-9]{9})/?', parsed.path)
                if parsed.scheme == 'https' and parsed.hostname == 'vc.feishu.cn' and match:
                    meeting_match = 'match' if match[1] == meeting_no else 'mismatch'
                else:
                    meeting_match = 'unknown_url_format'
            report['event'] = {'status': 'ok', 'calendar_id': calendar_id, 'event_id': event_id,
                'title': preview(event.get('summary', '')), 'description': description_preview,
                'description_rich': rich_preview,
                'attachments': {'status': 'ok' if 'attachments' in event else 'field_absent', 'count': len(attachments), 'items': listed,
                                'truncated': len(attachments) > MAX_ATTACHMENTS},
                'description_document_count': len(references), 'meeting_link_check': meeting_match}
            report['event_linkage'] = 'description_references_only' if event_keys else 'no_document_reference_found'
        except (ApiError, ValueError) as exc:
            if not isinstance(exc, ApiError):
                exc = ApiError(ErrorKind.RESPONSE)
            report['event'] = _failure(exc)
    seen = set()
    # Explicit URLs are verification targets; never promote them to event-derived references.
    candidates = expected + references
    report['document_limit'] = {'maximum': MAX_DOCUMENTS,
        'unique_discovered': len(expected_keys | event_keys),
        'truncated': len(expected_keys | event_keys) > MAX_DOCUMENTS}
    for reference in candidates:
        key = (reference['type'], reference['token'])
        if key in seen:
            continue
        seen.add(key)
        if len(report['documents']) >= MAX_DOCUMENTS:
            break
        result = probe_document(client, reference)
        result['origin'] = 'event_description' if key in event_keys else 'explicit_check_only'
        result['source_fields'] = reference_fields[key] if key in event_keys else []
        result['expected_linkage'] = ('found_in_description' if key in event_keys else
            'not_found_in_event' if report['event']['status'] == 'ok' else 'not_verified')
        report['documents'].append(result)
    return report


def save_report(report, directory):
    """One bounded private probe artifact; not a managed meeting database/log."""
    raw = (json.dumps(report, ensure_ascii=False, indent=2) + '\n').encode()
    if len(raw) > MAX_REPORT_BYTES:
        raise StorageError('probe report exceeds its size budget; no report saved')
    directory = Path(directory)
    private_directory(directory.parent)
    private_directory(directory)
    path = directory / (uuid4().hex + '.json')
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'wb') as out:
        out.write(raw)
        out.flush()
        os.fsync(out.fileno())
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read-only Feishu calendar / Docx / Wiki context probe')
    parser.add_argument('--config', type=Path, default=DEFAULT_CONFIG)
    parser.add_argument('--calendar-id')
    parser.add_argument('--event-id')
    parser.add_argument('--document-url', action='append', default=[], help='explicit verification target; does not prove event association')
    parser.add_argument('--meeting-no', help='optional nine-digit number for checking the event meeting link; never joins')
    parser.add_argument('--identity', choices=('application', 'user'), default='application')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--list-calendars', action='store_true', help='list one page of readable calendar metadata, without reading events')
    mode.add_argument('--find-event', action='store_true', help='match the known meeting link within one explicit calendar/time window')
    parser.add_argument('--page-token', help='manual calendar-list pagination')
    parser.add_argument('--start', help='ISO 8601 time with timezone, e.g. 2026-10-14T15:30:00+08:00')
    parser.add_argument('--end', help='ISO 8601 time with timezone, within one day of start')
    parser.add_argument('--save-report', action='store_true', help='save bounded private previews under data/context_probe; remove separately after review')
    args = parser.parse_args(argv)
    if args.find_event and (not args.calendar_id or not args.start or not args.end or not args.meeting_no or args.event_id):
        parser.error('--find-event requires --calendar-id, --start, --end, --meeting-no, and no --event-id')
    if not args.list_calendars and not args.find_event and (bool(args.calendar_id) != bool(args.event_id) or not args.calendar_id and not args.document_url):
        parser.error('provide --calendar-id with --event-id, or at least one --document-url')
    try:
        config = load_config(args.config)
        kwargs = {'timeout': config.timeout_seconds, 'request_seconds': config.request_seconds}
        client = (UserContextClient(config.credentials,
            oauth_manager=OAuthManager(config.credentials, config.data_directory), **kwargs)
            if args.identity == 'user' else ContextProbeClient(config.credentials, **kwargs))
        if args.list_calendars:
            report = client.calendars(args.page_token)
        elif args.find_event:
            report = client.find_event(args.calendar_id, args.start, args.end, args.meeting_no)
        else:
            report = probe(client, calendar_id=args.calendar_id, event_id=args.event_id,
                           document_urls=args.document_url, meeting_no=args.meeting_no)
        if args.save_report:
            path = save_report(report, config.data_directory / 'context_probe')
            print('Private probe report saved: ' + str(path), file=sys.stderr)
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        if args.list_calendars:
            return 2 if report['has_more'] else 0
        if args.find_event:
            return 0 if len(report['matches']) == 1 and not report['truncated_possible'] else 2
        failures = report['event']['status'] == 'failed' or any(item['status'] in ('failed', 'partial') for item in report['documents'])
        incomplete = (report['document_limit']['truncated'] or report['event'].get('meeting_link_check') == 'mismatch'
            or any(item['expected_linkage'] == 'not_found_in_event' or item['status'] == 'unsupported' for item in report['documents']))
        return 1 if failures else 2 if incomplete else 0
    except (ConfigError, ProbeInputError, StorageError, ApiError, OAuthError) as exc:
        print('Probe stopped: ' + str(exc), file=sys.stderr)
        return 1
    except OSError:
        print('Probe stopped: local file error; details hidden', file=sys.stderr)
        return 1
    except (KeyboardInterrupt, StopRequested):
        return 130


if __name__ == '__main__':
    sys.exit(main())
