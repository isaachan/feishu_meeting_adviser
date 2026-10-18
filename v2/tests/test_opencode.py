from dataclasses import replace
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from feishu_meeting_adviser.config import ConfigError, load_config
from feishu_meeting_adviser.diagnostics import sanitize
from feishu_meeting_adviser.llm import LLMSettings, ModelBackend, ModelError, create_backend
from feishu_meeting_adviser.opencode_backend import OpenCodeBackend, clean_runtime


class OpenCodeTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.settings=LLMSettings(backend='opencode',timeout_seconds=4)
        self.counter=0
        auth=patch.dict(os.environ,{'OPENCODE_AUTH_CONTENT':'{}'})
        auth.start();self.addCleanup(auth.stop)

    def backend(self,mode='ok',**kwargs):
        self.counter+=1
        capture=self.root/f'capture-{self.counter}.jsonl'
        peer=Path(__file__).parent/'helpers/fake_opencode_server.py'
        backend=OpenCodeBackend(self.settings,self.root/f'runtime-{self.counter}',
            command=[sys.executable,str(peer),mode,str(capture)],**kwargs)
        self.addCleanup(backend.close)
        return backend,capture

    def records(self,capture):
        return [json.loads(line) for line in capture.read_text().splitlines()]

    def test_opencode_implements_complete_model_backend_protocol(self):
        backend,_ = self.backend()
        self.assertIsInstance(backend, ModelBackend)
        self.assertGreater(backend.context_overhead_characters, 0)

    def test_role_snapshot_exact_model_and_schema_are_sent_and_session_is_deleted(self):
        backend,capture=self.backend('reply')
        snapshot={'captions':[{'sentence_key':'s:1','text':'synthetic question'}],
            'meeting_context':{'documents':[{'text':'synthetic document body'}]},'fresh_input_keys':['s:1']}
        result=backend.decide(snapshot,'Synthetic role instructions')
        self.assertEqual(result.reply,'SYNTHETIC GLM REPLY')
        records=self.records(capture)
        payload=next(r['payload'] for r in records if r['path'].endswith('prompt_async'))
        self.assertEqual(payload['model'],{'providerID':'zai-coding-plan','modelID':'glm-5.3'})
        self.assertEqual(payload['system'],'Synthetic role instructions')
        self.assertEqual(json.loads(payload['parts'][0]['text']),snapshot)
        self.assertNotIn('format',payload)
        self.assertNotIn('messageID',payload)
        self.assertEqual(records[-1]['method'],'DELETE')
        self.assertFalse(list(backend.workdir.glob('run-*')))
        self.assertIsNone(backend.process)

    def test_missing_auth_model_or_tool_isolation_fails_before_submitting_any_prompt(self):
        for mode,reason in [('auth-missing','model_authentication_required'),('model-missing','model_unavailable'),
                            ('plugin','model_tools_unavailable'),('permissions','model_tools_unavailable'),('mcp','model_tools_unavailable')]:
            with self.subTest(mode=mode):
                backend,capture=self.backend(mode)
                with self.assertRaises(ModelError) as caught:backend.decide({},'Synthetic role')
                self.assertEqual(caught.exception.reason,reason)
                self.assertFalse(any(r['method']=='POST' for r in self.records(capture)))
                self.assertFalse(list(backend.workdir.glob('run-*')))

    def test_invalid_truncated_unstructured_tool_or_cross_model_results_never_become_decisions(self):
        for mode,reason in [('invalid','model_invalid_decision'),('no-text','model_invalid_decision'),('fenced','model_invalid_decision'),
                            ('truncated','model_failed'),('error','model_failed'),('tool','model_tool_request'),
                            ('wrong-model','model_protocol'),('multiple','model_invalid_decision')]:
            with self.subTest(mode=mode):
                backend,_=self.backend(mode)
                with self.assertRaises(ModelError) as caught:backend.decide({},'Synthetic role')
                self.assertEqual(caught.exception.reason,reason)
                self.assertNotIn('private',str(caught.exception))
                self.assertIsNone(backend.process)

    def test_entire_deadline_covers_startup_generation_and_dripping_http_bodies(self):
        for mode in ('stall-start','timeout','drip','incomplete'):
            with self.subTest(mode=mode):
                backend,_=self.backend(mode)
                backend.settings=replace(self.settings,timeout_seconds=1.5)
                started=time.monotonic()
                with self.assertRaises(ModelError) as caught:backend.decide({},'Synthetic role')
                self.assertEqual(caught.exception.reason,'model_timeout')
                self.assertLess(time.monotonic()-started,4)
                self.assertFalse(list(backend.workdir.glob('run-*')))

    def test_short_disconnect_restarts_once_with_same_deadline(self):
        for mode in ('disconnect-once-verify','disconnect-once-generation'):
            with self.subTest(mode=mode):
                backend,capture=self.backend(mode)
                result=backend.decide({},'Synthetic role')
                self.assertIsNone(result.reply)
                self.assertEqual(backend.diagnostic_metadata()['opencode_transport_restarts'],1)
                self.assertFalse(list(backend.workdir.glob('run-*')))
                prompts=[r for r in self.records(capture) if r['path'].endswith('prompt_async')]
                self.assertEqual(len(prompts),1 if mode.endswith('verify') else 2)

    def test_repeated_disconnect_still_fails_bounded(self):
        backend,_=self.backend('disconnect-always')
        with self.assertRaises(ModelError) as caught:
            backend.decide({},'Synthetic role')
        self.assertEqual(caught.exception.reason,'model_disconnected')
        self.assertEqual(backend.diagnostic_metadata()['opencode_transport_restarts'],1)
        self.assertFalse(list(backend.workdir.glob('run-*')))

    def test_user_stop_interrupts_generation_and_removes_owned_server_and_data(self):
        stopping=threading.Event();backend,capture=self.backend('timeout',stop_event=stopping)
        def stop_after_submit():
            deadline=time.monotonic()+3
            while time.monotonic()<deadline:
                if capture.exists() and 'prompt_async' in capture.read_text():break
                time.sleep(.02)
            stopping.set()
        waiter=threading.Thread(target=stop_after_submit);waiter.start()
        self.addCleanup(waiter.join)
        with self.assertRaises(ModelError) as caught:backend.decide({},'Synthetic role')
        self.assertEqual(caught.exception.reason,'model_stopped')
        self.assertFalse(list(backend.workdir.glob('run-*')))
        pid=int(capture.with_suffix('.pid').read_text())
        with self.assertRaises(ProcessLookupError):os.kill(pid,0)

    def test_child_configuration_and_credentials_are_isolated_without_copying_other_logins(self):
        backend,_=self.backend()
        backend.runtime=self.root/'environment';backend.runtime.mkdir(mode=0o700);backend.password='synthetic-password'
        credentials={'zai-coding-plan':{'type':'api','key':'synthetic-selected-key'},
                     'openai':{'type':'oauth','access':'synthetic-other-token'}}
        with patch.dict(os.environ,{'OPENCODE_AUTH_CONTENT':json.dumps(credentials),
                'OPENCODE_CONFIG':'private-user-config','OPENCODE_CONFIG_DIR':'private-user-directory'}):
            env=backend._environment()
        self.assertEqual(json.loads(env['OPENCODE_AUTH_CONTENT']),{'zai-coding-plan':credentials['zai-coding-plan']})
        self.assertNotIn('OPENCODE_CONFIG',env);self.assertNotIn('OPENCODE_CONFIG_DIR',env)
        self.assertEqual(env['OPENCODE_DISABLE_DEFAULT_PLUGINS'],'true')
        self.assertEqual(env['XDG_DATA_HOME'],str(backend.runtime/'data'))
        self.assertFalse(any(p.is_file() for p in backend.runtime.rglob('*')))

    def test_model_configuration_defaults_cli_override_and_diagnostics_are_backend_specific(self):
        path=self.root/'config.yaml';path.write_text('app:\n  id: synthetic\n  secret: synthetic\nmeeting:\n  meeting_no: "012345678"\nllm:\n  backend: opencode\n');path.chmod(0o600)
        config=load_config(path)
        self.assertEqual(config.llm.opencode_model,'zai-coding-plan/glm-5.3')
        self.assertEqual(config.llm.executable,'codex')
        self.assertIsInstance(create_backend(config.llm,self.root/'factory'),OpenCodeBackend)
        path.write_text(path.read_text()+'  opencode_model: "missing-provider-prefix"\n')
        with self.assertRaises(ConfigError):load_config(path)
        safe=sanitize({'kind':'model','backend':'opencode','model_stage':'server_verify',
            'model_input_tokens':10,'opencode_polls':2,'prompt':'private','api_key':'private'})
        self.assertEqual(safe,{'kind':'model','backend':'opencode','model_stage':'server_verify','model_input_tokens':10,'opencode_polls':2})

    def test_sigkill_of_consumer_still_stops_server_and_cleans_temporary_store(self):
        peer=Path(__file__).parent/'helpers/fake_opencode_server.py'
        capture=self.root/'killed.jsonl';runtime=self.root/'killed-runtime'
        program='''
import sys
from pathlib import Path
from feishu_meeting_adviser.llm import LLMSettings
from feishu_meeting_adviser.opencode_backend import OpenCodeBackend
b=OpenCodeBackend(LLMSettings(backend='opencode',timeout_seconds=30),Path(sys.argv[1]),
 command=[sys.executable,sys.argv[2],'timeout',sys.argv[3]])
b.decide({},'Synthetic role')
'''
        consumer=subprocess.Popen([sys.executable,'-c',program,str(runtime),str(peer),str(capture)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.addCleanup(lambda:consumer.kill() if consumer.poll() is None else None)
        deadline=time.monotonic()+6
        while not capture.exists() and time.monotonic()<deadline:time.sleep(.02)
        self.assertTrue(capture.exists())
        os.kill(consumer.pid,signal.SIGKILL);consumer.wait(timeout=3)
        pid=int(capture.with_suffix('.pid').read_text())
        deadline=time.monotonic()+4
        while list(runtime.glob('run-*')) and time.monotonic()<deadline:time.sleep(.02)
        self.assertFalse(list(runtime.glob('run-*')))
        with self.assertRaises(ProcessLookupError):os.kill(pid,0)

    def test_abandoned_runtime_is_cleaned_without_removing_unrelated_files(self):
        root=self.root/'runtime';root.mkdir(mode=0o700)
        run=root/'run-abandoned';run.mkdir(mode=0o700)
        (run/'synthetic.db').write_text('synthetic private prompt')
        (root/'keep.txt').write_text('unrelated')
        clean_runtime(root)
        self.assertFalse(run.exists());self.assertTrue((root/'keep.txt').exists())


if __name__=='__main__':unittest.main()
