"""Synthetic app-server peer. Never calls a model or any meeting API."""
import json
import os
from pathlib import Path
import sys
import time

mode = sys.argv[1]
if len(sys.argv)>2:
    Path(sys.argv[2]).write_text(str(os.getpid()))
index = 0
thread_config = {}


def emit(value):
    print(json.dumps(value), flush=True)


def event(method, **params):
    emit({'method': method, 'params': params})


for line in sys.stdin:
    request = json.loads(line)
    if 'id' not in request:
        continue
    method, params = request.get('method'), request.get('params', {})
    if mode == 'slow-start':
        time.sleep(0.12)
    result = {}
    if method == 'account/read':
        result={'account':{'type':'apiKey' if mode=='apikey' else 'chatgpt'}}
    elif method == 'mcpServerStatus/list':
        if mode == 'verify-stall' and params.get('threadId'):
            time.sleep(10)
            continue
        inherited = mode in ('inherited','exposed','cached','empty-connected','unknown','starting','late-server')
        data = []
        if inherited and (params.get('threadId') or mode != 'late-server'):
            runtime = {'exposed':'connected', 'cached':'disabled', 'empty-connected':'connected',
                       'unknown':None, 'starting':'starting', 'late-server':'connected'}.get(mode, 'disabled')
            tools = {'synthetic-private-tool': {}} if not params.get('threadId') or mode in ('exposed','cached') else {}
            data = [{'name':'synthetic-private-server','tools':tools,'runtimeStatus':runtime}]
        if mode in ('late-disabled','changing-servers') and params.get('threadId'):
            name = 'synthetic-late-server' if mode == 'late-disabled' else f'synthetic-late-{index}'
            disabled = thread_config.get('mcp_servers',{}).get(name,{}).get('enabled') is False
            data = [{'name':name,'pluginId':'synthetic-plugin','tools':{},
                     'runtimeStatus':'disabled' if disabled else 'connected'}]
        result={'data':data,'nextCursor':None}
    elif method == 'thread/start':
        assert params['ephemeral'] is True and params['sandbox'] == 'read-only'
        assert params['modelProvider']=='openai'
        if mode in ('inherited','exposed','cached','empty-connected','unknown','starting'):
            assert params['config']['mcp_servers']['synthetic-private-server'] == {'command':'/usr/bin/false','enabled':False}
        index += 1
        thread_config = params['config']
        thread_id = f'synthetic-thread-{index}'
        result = {'thread': {'id': thread_id}, 'model': 'synthetic-model', 'reasoningEffort':'high'}
    elif method == 'turn/start':
        if mode == 'late-disabled':
            assert index == 2
            assert thread_config['mcp_servers']['synthetic-late-server']['enabled'] is False
            assert thread_config['plugins']['synthetic-plugin']['enabled'] is False
        assert mode not in ('late-server','changing-servers','empty-connected','unknown','starting','exposed')
        thread_id = params['threadId']
        turn_id = f'synthetic-turn-{index}'
        assert params['outputSchema']['additionalProperties'] is False
        assert params['sandboxPolicy']['networkAccess'] is False
        if mode == 'malformed':
            print('not json private-secret', flush=True)
            continue
        if mode == 'tool':
            emit({'id': 'synthetic-request', 'method': 'item/tool/requestUserInput', 'params': {}})
            continue
        if mode == 'delay':
            time.sleep(10)
            continue
        if mode == 'generation-stall':
            emit({'id': request['id'], 'result': {'turn': {'id': turn_id}}})
            time.sleep(10)
            continue
        body = {'control': 'none', 'control_sentence_keys': [], 'reply': None, 'reply_sentence_keys': []}
        if mode=='reply':
            body.update(reply='SYNTHETIC MODEL REPLY',reply_sentence_keys=['s:1'])
        text = json.dumps(body) if mode != 'invalid' else 'private malformed output'
        item = {'id': 'synthetic-message', 'type': 'agentMessage', 'phase': 'final_answer', 'text': text}
        # Output may arrive before the RPC response; unrelated threads must be ignored.
        event('item/completed', threadId='other-thread', turnId=turn_id,
              item={'id': 'other', 'type': 'agentMessage', 'text': 'wrong'})
        event('item/completed', threadId=thread_id, turnId=turn_id, item=item)
        result = {'turn': {'id': turn_id}}
        emit({'id': request['id'], 'result': result})
        items=[item]+([{'id':'hidden-call','type':'mcpToolCall'}] if mode=='late-tool' else [])
        event('turn/completed', threadId=thread_id, turn={'id': turn_id,
              'status': 'failed' if mode == 'failed' else 'completed', 'items': items})
        continue
    emit({'id': request['id'], 'result': result})
    if mode=='write-stall' and method=='mcpServerStatus/list' and params.get('threadId'):
        time.sleep(10)
