"""Process fault fixture: fake Feishu and fake model transport, no external requests."""
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

from feishu_meeting_adviser.__main__ import main
from feishu_meeting_adviser.collector import utc_now
from feishu_meeting_adviser.config import load_config
from feishu_meeting_adviser.lifecycle import SessionManager
from feishu_meeting_adviser.llm import CodexBackend
from feishu_meeting_adviser.models import Caption, SourceState, Status
from feishu_meeting_adviser.storage import Store

action, root = sys.argv[1], Path(sys.argv[2])
mid='987654321012345678'
path=root/'config.yaml'
if not path.exists():
    path.write_text('app:\n  id: synthetic\n  secret: synthetic\nmeeting:\n  meeting_no: "012345678"\n'
                    'llm:\n  backend: codex\n  batch_seconds: 0\n  introduction: false\n  timeout_seconds: 30\n')
    path.chmod(0o600)


class API:
    def join(self,number,password):
        return {'data':{'meeting':{'id':mid,'meeting_no':number},'join_user':{'id':'synthetic-bot'}}}
    def leave(self,meeting_id):
        (root/'unexpected-leave').write_text('called')
        return {'data':{}}
    def send_message(self,meeting_id,text,message_uuid):
        with (root/'sent.jsonl').open('a') as stream:
            stream.write(json.dumps({'uuid':message_uuid,'text':text})+'\n')
            stream.flush()
            os.fsync(stream.fileno())
        if action=='crash-post':
            os._exit(99)


api=API()
config=load_config(path)
source=Store(config.data_directory/'source.sqlite3')
manager=SessionManager(source,config,api)
manager.join()
source.ensure_meeting(mid,{})
if source.caption(mid,'s:1') is None:
    stamp=utc_now()
    source.commit_page(mid,[Caption(mid,'s:1','SYNTHETIC MODEL QUESTION',stamp,event_time=stamp,speaker_id='synthetic-person')],{})
source.publish_status(Status(mid,SourceState.COLLECTING,'scan_success',utc_now()))
source.close()

def backend(settings,workdir,*,stop_event):
    peer=Path(__file__).with_name('fake_codex_server.py')
    return CodexBackend(settings,workdir,stop_event=stop_event,
        command=[sys.executable,str(peer),'delay' if action=='watch' else 'reply',str(root/'peer-pid')])

with patch('feishu_meeting_adviser.__main__.FeishuClient',return_value=api), \
     patch('feishu_meeting_adviser.adviser.CodexBackend',side_effect=backend):
    args=['--config',str(path),'consume']
    if action=='watch':
        args.append('--watch')
    raise SystemExit(main(args))
