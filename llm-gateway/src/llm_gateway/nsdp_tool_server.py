"""Task-scoped, read-only NSDP MCP server over stdio.

The owner is supplied by the gateway, never by tool arguments. Credentials are
fetched only on tool invocation and passed directly to the noam subprocess.
Tool durations are appended to the gateway-provided timing file (name, status,
ms only; never arguments or results).
"""
import argparse
import json
import re
import secrets
import sqlite3
import sys
import time
from pathlib import Path
from urllib import error, parse, request

from ._private import LockError
from .nsdp_cache import OwnerReleaseCache
from .nsdp_query import NsdpQuery, QueryError, _RELEASE

_OWNER = re.compile(r'ou_[A-Za-z0-9_-]{1,128}\Z')


def timing_recorder(path: str | None):
    """Append content-free tool timings and stage events."""
    class Recorder:
        def __init__(self, target):
            self.target = Path(target) if target else None

        def _write(self, value):
            if self.target is None:
                return
            try:
                with self.target.open('a') as out:
                    out.write(json.dumps(value, separators=(',', ':')) + '\n')
            except OSError:
                pass

        def __call__(self, tool, status, elapsed, **metrics):
            self._write({'tool': tool, 'status': status,
                         'ms': int(elapsed * 1000), **metrics})

        def stage(self, call_id, stage, **fields):
            self._write({'event': 'nsdp_stage', 'call_id': call_id,
                         'stage': stage, 'at_ns': time.time_ns(), **fields})

    return Recorder(path)


def credential_provider(base_url, provider='nsdp'):
    if provider not in ('nsdp', 'jira'):
        raise ValueError('invalid provider')
    opener = request.build_opener(request.ProxyHandler({}))

    def get(owner):
        if not _OWNER.fullmatch(owner):
            raise QueryError('owner_missing')
        url = (base_url.rstrip('/') + '/credentials/' + parse.quote(owner, safe='')
               + '/token?provider=' + provider)
        try:
            with opener.open(url, timeout=10) as response:
                body = response.read(65537)
            if len(body) > 65536:
                raise QueryError('unavailable')
            data = json.loads(body)
            token = data.get('token') if isinstance(data, dict) else None
            if not isinstance(token, str) or not token:
                raise QueryError('unavailable')
            return token
        except error.HTTPError as exc:
            code = exc.code
            exc.close()
            raise QueryError('authorization_required' if code == 404 else 'unavailable') from None
        except (error.URLError, OSError, ValueError):
            raise QueryError('unavailable') from None

    return get


def handle(message, *, owner, query, record=None, cache=None,
           cache_unavailable=False, deadline=None):
    """Handle one MCP JSON-RPC request; notifications have no response."""
    if not isinstance(message, dict) or 'id' not in message:
        return None
    request_id = message['id']
    method = message.get('method')
    if method == 'initialize':
        result = {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}},
                  'serverInfo': {'name': 'meeting-nsdp', 'version': '1.0'}}
    elif method == 'ping':
        result = {}
    elif method == 'tools/list':
        result = {'tools': [{'name': 'lookup_release',
                            'description': 'Look up a release and its NSDP milestones. '
                                           'The release is a search filter, not an exact match.',
                            'inputSchema': {'type': 'object', 'additionalProperties': False,
                                            'properties': {'release': {'type': 'string'}},
                                            'required': ['release']}}]}
    elif method == 'tools/call':
        params = message.get('params')
        if not isinstance(params, dict) or params.get('name') != 'lookup_release':
            return {'jsonrpc': '2.0', 'id': request_id,
                    'error': {'code': -32602, 'message': 'unknown tool'}}
        args = params.get('arguments')
        if not isinstance(args, dict) or set(args) != {'release'}:
            return {'jsonrpc': '2.0', 'id': request_id,
                    'error': {'code': -32602, 'message': 'invalid arguments'}}
        started = time.monotonic()
        call_id = secrets.token_hex(6)
        emit = getattr(record, 'stage', lambda *_args, **_kwargs: None)
        emit(call_id, 'mcp_received')
        metrics = {}
        if cache_unavailable:
            emit(call_id, 'cache_unavailable')

        def on_noam(stage, status, ms):
            if stage == 'start':
                emit(call_id, 'noam_start')
            else:
                metrics['noam_ms'] = ms
                emit(call_id, 'noam_done', status=status, ms=ms)

        try:
            release = args['release']
            if cache is None:
                text = query.releases(owner, release)
            else:
                if not isinstance(release, str) or not _RELEASE.fullmatch(release):
                    raise QueryError('invalid_release')
                emit(call_id, 'auth_start')
                auth_started = time.monotonic()
                try:
                    token = query.credential_provider(owner)
                finally:
                    metrics['auth_ms'] = int((time.monotonic() - auth_started) * 1000)
                    emit(call_id, 'auth_done', ms=metrics['auth_ms'])
                if not isinstance(token, str) or not token:
                    raise QueryError('authorization_required')
                try:
                    text, hit, age_ms, wait_ms = cache.lookup(
                        owner, release, token,
                        lambda: query.releases(owner, release, token=token,
                                               on_noam=on_noam), deadline=deadline,
                        on_miss=lambda: emit(call_id, 'cache_miss'))
                    metrics.update(cache_hit=hit, cache_wait_ms=wait_ms)
                    if age_ms is not None:
                        metrics['cache_age_ms'] = age_ms
                    if hit:
                        emit(call_id, 'cache_hit', ms=wait_ms)
                except (OSError, sqlite3.Error, LockError):
                    emit(call_id, 'cache_error')
                    text = query.releases(owner, release, token=token,
                                          on_noam=on_noam)
            result = {'content': [{'type': 'text', 'text': text}], 'isError': False}
            if record is not None:
                record('meeting_nsdp_lookup_release', 'ok', time.monotonic() - started,
                       **metrics)
        except QueryError as exc:
            if cache is not None and exc.code == 'authorization_required':
                try:
                    cache.invalidate(owner)
                except (OSError, sqlite3.Error, LockError):
                    emit(call_id, 'cache_error')
            result = {'content': [{'type': 'text', 'text': 'NSDP query failed: ' + exc.code}],
                      'isError': True}
            if record is not None:
                record('meeting_nsdp_lookup_release', 'error', time.monotonic() - started,
                       **metrics)
        emit(call_id, 'mcp_response_ready', status='error' if result['isError'] else 'ok')
    else:
        return {'jsonrpc': '2.0', 'id': request_id,
                'error': {'code': -32601, 'message': 'method not found'}}
    return {'jsonrpc': '2.0', 'id': request_id, 'result': result}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--owner', required=True)
    parser.add_argument('--auth-url', required=True)
    parser.add_argument('--noam-bin', default='noam')
    parser.add_argument('--timing-file', default=None)
    parser.add_argument('--cache-db', type=Path, default=None)
    parser.add_argument('--deadline-monotonic', type=float, default=None)
    args = parser.parse_args()
    if not _OWNER.fullmatch(args.owner):
        parser.error('invalid owner')
    query = NsdpQuery(credential_provider(args.auth_url), noam_bin=args.noam_bin)
    record = timing_recorder(args.timing_file)
    cache = None
    cache_unavailable = False
    if args.cache_db is not None:
        try:
            cache = OwnerReleaseCache(args.cache_db)
        except (OSError, sqlite3.Error, LockError):
            cache_unavailable = True
    try:
        for line in sys.stdin:
            try:
                response = handle(json.loads(line), owner=args.owner, query=query,
                                  record=record, cache=cache,
                                  cache_unavailable=cache_unavailable,
                                  deadline=args.deadline_monotonic)
                if response is not None:
                    print(json.dumps(response, ensure_ascii=False), flush=True)
            except (ValueError, TypeError):
                print(json.dumps({'jsonrpc': '2.0', 'id': None,
                                  'error': {'code': -32700, 'message': 'invalid request'}}),
                      flush=True)
    finally:
        if cache is not None:
            cache.close()


if __name__ == '__main__':
    main()
