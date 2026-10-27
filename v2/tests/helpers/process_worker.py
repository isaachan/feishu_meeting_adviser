"""Synthetic subprocess-only fault driver. Never contacts Feishu."""
import os
from pathlib import Path
import sys
from unittest.mock import patch

from feishu_meeting_adviser.__main__ import main
from feishu_meeting_adviser.config import load_config
from feishu_meeting_adviser.lifecycle import SessionManager
from feishu_meeting_adviser.models import Caption
from feishu_meeting_adviser.storage import DurableFakeConsumer, Store

MID = '987654321012345678'
root = Path(sys.argv[2])
action = sys.argv[1]

if action == 'crash-page':
    Store.initialize(root / 'data' / 'source.sqlite3')
    store = Store(root / 'data' / 'source.sqlite3')
    store.ensure_meeting('synthetic-meeting', {'cursor':None})
    def fault(point):
        if point == sys.argv[3]:
            os._exit(97)
    store.commit_page('synthetic-meeting', [Caption('synthetic-meeting','s:1','synthetic input','2026-10-13T00:00:00Z')],
                      {'cursor':'next'}, fault=fault)
    raise SystemExit('fault point was not reached')

if action == 'crash-consumer':
    store = Store(root / 'data' / 'source.sqlite3')
    consumer = DurableFakeConsumer(root / 'data' / 'consumer.sqlite3')
    delivery = store.read('synthetic-meeting','qa')[0]
    consumer.accept(delivery)
    os._exit(98)  # Consumer persisted, source acknowledgement never happened.

if action in ('source','source-once'):
    path = root / 'config.yaml'
    if not path.exists():
        path.write_text('app:\n  id: synthetic-app\n  secret: synthetic-secret\nhttp:\n  timeout_seconds: 1\n')
        path.chmod(0o600)
    class API:
        def __init__(self):
            self.calls = 0
        def join(self, number, password):
            return {'data':{'meeting':{'id':MID,'meeting_no':number},'join_user':{'id':'synthetic-bot'}}}
        def leave(self, meeting_id):
            (root / 'unexpected-leave').write_text('called')
            return {'data':{}}
        def events(self, query):
            self.calls += 1
            if self.calls == 2:
                print('READY',flush=True)  # Previous cycle is already committed.
            return {'data':{'has_more':False,'events':[{'event_id':'fixture-event','event_type':'transcript_received',
                    'event_time':'2026-10-13T00:00:00.100Z','payload':{'text':'synthetic input','sentence_id':'s1'}}]}}
    api = API()
    config = load_config(path)
    Store.initialize(config.data_directory / 'source.sqlite3')
    store = Store(config.data_directory / 'source.sqlite3')
    manager = SessionManager(store,config,api)
    manager.join('012345678')  # Existing joined session is reused when restarting.
    store.close()
    with patch('feishu_meeting_adviser.__main__.FeishuClient',return_value=api), \
         patch('feishu_meeting_adviser.lifecycle.FeishuClient',return_value=api):
        args = ['--config',str(path),'collect','--no-text']
        if action == 'source-once':
            args.append('--once')
        raise SystemExit(main(args))
