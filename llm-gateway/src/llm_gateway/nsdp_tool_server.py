"""Task-scoped, read-only NSDP MCP server over stdio.

The owner is supplied by the gateway, never by tool arguments. Credentials are
fetched only on tool invocation and passed directly to the noam subprocess.
"""
import argparse
import json
import re
import sys
from urllib import error, parse, request

from .nsdp_query import NsdpQuery, QueryError

_OWNER = re.compile(r'ou_[A-Za-z0-9_-]{1,128}\Z')


def credential_provider(base_url):
    opener = request.build_opener(request.ProxyHandler({}))

    def get(owner):
        if not _OWNER.fullmatch(owner):
            raise QueryError('owner_missing')
        url = (base_url.rstrip('/') + '/credentials/' + parse.quote(owner, safe='')
               + '/token?provider=nsdp')
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


def handle(message, *, owner, query):
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
        try:
            text = query.releases(owner, args['release'])
            result = {'content': [{'type': 'text', 'text': text}], 'isError': False}
        except QueryError as exc:
            result = {'content': [{'type': 'text', 'text': 'NSDP query failed: ' + exc.code}],
                      'isError': True}
    else:
        return {'jsonrpc': '2.0', 'id': request_id,
                'error': {'code': -32601, 'message': 'method not found'}}
    return {'jsonrpc': '2.0', 'id': request_id, 'result': result}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--owner', required=True)
    parser.add_argument('--auth-url', required=True)
    parser.add_argument('--noam-bin', default='noam')
    args = parser.parse_args()
    if not _OWNER.fullmatch(args.owner):
        parser.error('invalid owner')
    query = NsdpQuery(credential_provider(args.auth_url), noam_bin=args.noam_bin)
    for line in sys.stdin:
        try:
            response = handle(json.loads(line), owner=args.owner, query=query)
            if response is not None:
                print(json.dumps(response, ensure_ascii=False), flush=True)
        except (ValueError, TypeError):
            print(json.dumps({'jsonrpc': '2.0', 'id': None,
                              'error': {'code': -32700, 'message': 'invalid request'}}),
                  flush=True)


if __name__ == '__main__':
    main()
