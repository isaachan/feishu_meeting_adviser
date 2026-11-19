"""Task-scoped Jira read-only MCP tools. No model-controlled credentials or URL."""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit

from .nsdp_query import QueryError
from .nsdp_tool_server import _OWNER, credential_provider, timing_recorder

_KEY = re.compile(r'[A-Za-z][A-Za-z0-9_]{0,63}-[0-9]{1,12}\Z')


class JiraQuery:
    def __init__(self, credential, *, url, noam_bin='noam', run=subprocess.run, timeout=60):
        self.credential, self.url, self.noam_bin = credential, url, noam_bin
        self.run, self.timeout = run, timeout

    def execute(self, owner, name, args):
        if not isinstance(args, dict):
            raise QueryError('invalid_arguments')
        if name == 'get_issue' and set(args) == {'key'}:
            key = args['key']
            if not isinstance(key, str) or not _KEY.fullmatch(key):
                raise QueryError('invalid_arguments')
            command = ['issue', 'get', key, '--expand', 'changelog']
        elif name == 'search_issues' and set(args) <= {'jql', 'start'} and 'jql' in args:
            jql, start = args['jql'], args.get('start', 0)
            if (not isinstance(jql, str) or not jql.strip() or len(jql) > 4000
                    or '\x00' in jql or type(start) is not int or not 0 <= start <= 10000):
                raise QueryError('invalid_arguments')
            command = ['issue', 'search', jql, '--max', '50', '--start', str(start)]
        elif name == 'fields' and set(args) == {'search'}:
            search = args['search']
            if not isinstance(search, str) or not search.strip() or len(search) > 128:
                raise QueryError('invalid_arguments')
            command = ['fields', '--search', search]
        elif name == 'projects' and not args:
            command = ['projects']
        else:
            raise QueryError('invalid_arguments')
        url = urlsplit(self.url)
        if url.scheme != 'https' or not url.netloc or url.username or url.password or url.query or url.fragment:
            raise QueryError('unavailable')
        token = self.credential(owner)
        with tempfile.TemporaryDirectory(prefix='gateway-jira-') as directory:
            env = dict(os.environ)
            env['HOME'] = directory
            for category in ('CONFIG', 'DATA', 'CACHE', 'STATE'):
                env['XDG_' + category + '_HOME'] = str(Path(directory) / category.lower())
            env.pop('NSDP_TOKEN', None)
            env.update(JIRA_URL=self.url, JIRA_TOKEN=token, JIRA_READ_ONLY='true')
            try:
                result = self.run([self.noam_bin, 'jira', *command], env=env,
                                  capture_output=True, text=True, encoding='utf-8',
                                  errors='replace', timeout=self.timeout, stdin=subprocess.DEVNULL)
            except subprocess.TimeoutExpired:
                raise QueryError('timeout') from None
            except OSError:
                raise QueryError('unavailable') from None
        stdout, stderr = result.stdout or '', result.stderr or ''
        if result.returncode:
            text = stdout + stderr
            if re.search(r'(?<!\d)401(?!\d)', text):
                raise QueryError('authorization_required')
            if re.search(r'(?<!\d)403(?!\d)', text):
                raise QueryError('forbidden')
            raise QueryError('unavailable')
        if not stdout.strip() or token in stdout:
            raise QueryError('unavailable')
        if len(stdout.encode('utf-8')) > 100000:
            raise QueryError('output_too_large')
        try:
            json.loads(stdout)
        except ValueError:
            raise QueryError('output_invalid') from None
        return stdout.strip()


_TOOLS = [
    ('get_issue', 'Read an issue and its changelog', {'key': {'type': 'string'}}, ['key']),
    ('search_issues', 'Search issues using JQL, up to 50 per page',
     {'jql': {'type': 'string'}, 'start': {'type': 'integer'}}, ['jql']),
    ('fields', 'Search Jira field definitions by name', {'search': {'type': 'string'}}, ['search']),
    ('projects', 'List visible Jira projects', {}, []),
]


def handle(message, *, owner, query, record=None):
    if not isinstance(message, dict) or 'id' not in message:
        return None
    request_id, method = message['id'], message.get('method')
    if method == 'initialize':
        result = {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}},
                  'serverInfo': {'name': 'meeting-jira', 'version': '1.0'}}
    elif method == 'ping':
        result = {}
    elif method == 'tools/list':
        result = {'tools': [{'name': name, 'description': description,
                             'inputSchema': {'type': 'object', 'additionalProperties': False,
                                             'properties': properties, 'required': required}}
                            for name, description, properties, required in _TOOLS]}
    elif method == 'tools/call':
        params = message.get('params')
        if not isinstance(params, dict) or params.get('name') not in {t[0] for t in _TOOLS}:
            return {'jsonrpc': '2.0', 'id': request_id,
                    'error': {'code': -32602, 'message': 'unknown tool'}}
        started = time.monotonic()
        try:
            text = query.execute(owner, params['name'], params.get('arguments', {}))
            result = {'content': [{'type': 'text', 'text': text}], 'isError': False}
            if record is not None:
                record('meeting_jira_' + params['name'], 'ok', time.monotonic() - started)
        except QueryError as exc:
            result = {'content': [{'type': 'text', 'text': 'Jira query failed: ' + exc.code}],
                      'isError': True}
            if record is not None:
                record('meeting_jira_' + params['name'], 'error', time.monotonic() - started)
    else:
        return {'jsonrpc': '2.0', 'id': request_id,
                'error': {'code': -32601, 'message': 'method not found'}}
    return {'jsonrpc': '2.0', 'id': request_id, 'result': result}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--owner', required=True)
    parser.add_argument('--auth-url', required=True)
    parser.add_argument('--noam-bin', default='noam')
    parser.add_argument('--jira-url', default='')
    parser.add_argument('--timing-file', default=None)
    args = parser.parse_args()
    if not _OWNER.fullmatch(args.owner):
        parser.error('invalid owner')
    query = JiraQuery(credential_provider(args.auth_url, 'jira'), url=args.jira_url,
                      noam_bin=args.noam_bin)
    record = timing_recorder(args.timing_file)
    for line in sys.stdin:
        try:
            response = handle(json.loads(line), owner=args.owner, query=query,
                              record=record)
            if response is not None:
                print(json.dumps(response, ensure_ascii=False), flush=True)
        except (ValueError, TypeError):
            print(json.dumps({'jsonrpc': '2.0', 'id': None,
                              'error': {'code': -32700, 'message': 'invalid request'}}), flush=True)


if __name__ == '__main__':
    main()
