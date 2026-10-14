"""Synthetic app-server peer. Never calls a model or any meeting API."""
import json
import sys
import time

mode = sys.argv[1]
index = 0


def emit(value):
    print(json.dumps(value), flush=True)


def event(method, **params):
    emit({'method': method, 'params': params})


for line in sys.stdin:
    request = json.loads(line)
    if 'id' not in request:
        continue
    method, params = request.get('method'), request.get('params', {})
    result = {}
    if method == 'thread/start':
        assert params['ephemeral'] is True and params['sandbox'] == 'read-only'
        index += 1
        thread_id = f'synthetic-thread-{index}'
        result = {'thread': {'id': thread_id}, 'model': 'synthetic-model'}
    elif method == 'turn/start':
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
        body = {'control': 'none', 'control_sentence_keys': [], 'reply': None, 'reply_sentence_keys': []}
        text = json.dumps(body) if mode != 'invalid' else 'private malformed output'
        item = {'id': 'synthetic-message', 'type': 'agentMessage', 'phase': 'final_answer', 'text': text}
        # Output may arrive before the RPC response; unrelated threads must be ignored.
        event('item/completed', threadId='other-thread', turnId=turn_id,
              item={'id': 'other', 'type': 'agentMessage', 'text': 'wrong'})
        event('item/completed', threadId=thread_id, turnId=turn_id, item=item)
        result = {'turn': {'id': turn_id}}
        emit({'id': request['id'], 'result': result})
        event('turn/completed', threadId=thread_id, turn={'id': turn_id,
              'status': 'failed' if mode == 'failed' else 'completed', 'items': [item]})
        continue
    emit({'id': request['id'], 'result': result})
