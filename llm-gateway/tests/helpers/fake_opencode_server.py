"""Synthetic HTTP peer. No provider calls, real credentials or meeting data."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import time

mode = sys.argv[1]
capture = Path(sys.argv[2]) if len(sys.argv)>2 else None
config = json.loads(os.environ['OPENCODE_CONFIG_CONTENT'])
model = config['model'].split('/',1)
posted = None
sessions = polls = 0
if capture:
    capture.with_suffix('.pid').write_text(str(os.getpid()))


def record(value):
    if capture:
        with capture.open('a') as out:
            out.write(json.dumps(value)+'\n')


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args):
        pass

    def send(self,value,status=200):
        body=json.dumps(value).encode() if value is not None else b''
        self.send_response(status)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        global polls
        auth='Basic '+base64.b64encode(('meeting-adviser:'+os.environ['OPENCODE_SERVER_PASSWORD']).encode()).decode()
        assert self.headers['Authorization']==auth
        record({'method':'GET','path':self.path})
        if self.path=='/global/health':
            return self.send({'healthy':True,'version':'synthetic'})
        if self.path=='/config':
            if mode in ('disconnect-once-verify','disconnect-always'):
                marker=capture.with_suffix('.disconnected') if capture else None
                if mode=='disconnect-always' or marker and not marker.exists():
                    if marker:marker.touch()
                    os._exit(1)
            value=config.copy()
            if mode=='plugin':value['plugin']=['synthetic-plugin']
            return self.send(value)
        if self.path=='/agent':
            rules=[{'permission':'*','pattern':'*','action':'deny'}]
            if mode=='permissions':rules.append({'permission':'bash','pattern':'*','action':'allow'})
            return self.send([{'name':'meeting-decision','permission':rules}])
        if self.path=='/mcp':
            return self.send({'synthetic':{'status':'connected'}} if mode=='mcp' else {})
        if self.path=='/provider':
            return self.send({'all':[{'id':model[0],'models':{model[1]:{}} if mode!='model-missing' else {}}],
                'connected':[model[0]] if mode!='auth-missing' else []})
        if self.path=='/experimental/tool/ids':
            return self.send(['bash','read','edit','task','skill','webfetch'])
        if self.path.endswith('/message'):
            polls+=1
            if mode=='disconnect-once-generation':
                marker=capture.with_suffix('.disconnected') if capture else None
                if marker and not marker.exists():
                    marker.touch()
                    os._exit(1)
            if mode=='drip':
                self.send_response(200);self.end_headers()
                for _ in range(100):
                    self.wfile.write(b' ');self.wfile.flush();time.sleep(.05)
                return
            if mode=='timeout' or polls<2:return self.send([])
            decision={'control':'none','control_sentence_keys':[],'reply':None,'reply_sentence_keys':[]}
            if mode=='reply':decision.update(reply='SYNTHETIC GLM REPLY',reply_sentence_keys=['s:1'])
            if mode=='invalid':decision['unexpected']='private synthetic output'
            info={'id':'msg_fake','sessionID':'ses_fake1','role':'assistant','parentID':'msg_user',
                'providerID':model[0],'modelID':'other' if mode=='wrong-model' else model[1],
                'time':{'completed':1},'finish':'stop','structured':decision,
                'tokens':{'input':10,'output':5,'reasoning':0}}
            parts=[{'type':'text','text':json.dumps(decision)}]
            if mode=='tool':parts=[{'type':'tool','tool':'bash','state':{'status':'running'}}]
            if mode=='error':info['error']={'name':'APIError','data':{'message':'private synthetic provider failure'}}
            if mode=='no-text':parts=[]
            if mode=='fenced':parts=[{'type':'text','text':'```json\n'+json.dumps(decision)+'\n```'}]
            if mode=='truncated':info['finish']='length'
            if mode=='incomplete':info['time']={}
            item={'info':info,'parts':parts}
            user={'info':{'role':'user','id':'msg_user','sessionID':'ses_fake1'},'parts':[]}
            return self.send([user,item,item] if mode=='multiple' else [user,item])
        return self.send({},404)

    def do_POST(self):
        global posted,sessions
        body=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))) or '{}')
        record({'method':'POST','path':self.path,'payload':body})
        if self.path=='/session':
            sessions+=1
            return self.send({'id':'ses_fake1'})
        if self.path.endswith('/prompt_async'):
            posted=body
            assert body['agent']=='meeting-decision'
            assert 'format' not in body
            assert 'additionalProperties' in config['agent']['meeting-decision']['prompt']
            assert all(enabled is False for enabled in body['tools'].values())
            return self.send(None,204)
        return self.send(True)

    def do_DELETE(self):
        record({'method':'DELETE','path':self.path})
        return self.send(True)


if mode=='stall-start':
    time.sleep(100)
else:
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    print(f'opencode server listening on http://127.0.0.1:{server.server_port}',flush=True)
    server.serve_forever()
