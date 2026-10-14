from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest

from feishu_meeting_adviser.config import ConfigError, load_config
from feishu_meeting_adviser.llm import CodexBackend, Decision, LLMSettings, ModelError, load_role


class LLMTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = LLMSettings(timeout_seconds=0.3)

    def backend(self, mode='ok', **kwargs):
        peer = Path(__file__).parent / 'helpers' / 'fake_codex_server.py'
        backend = CodexBackend(self.settings, self.root / 'runtime', command=[sys.executable, str(peer), mode], **kwargs)
        self.addCleanup(backend.close)
        return backend

    def test_structured_decisions_reject_invalid_or_ambiguous_actions(self):
        silent = {'control': 'none', 'control_sentence_keys': [], 'reply': None, 'reply_sentence_keys': []}
        self.assertIsNone(Decision.parse(silent).reply)
        self.assertEqual(Decision.parse(silent | {'reply': '合成回复', 'reply_sentence_keys': ['s:1']}).reply, '合成回复')
        for bad in (None, {}, silent | {'extra': True}, silent | {'control': 'leave'},
                    silent | {'control': 'pause'}, silent | {'control_sentence_keys': ['s:1']},
                    silent | {'reply': 'reply'}, silent | {'reply_sentence_keys': ['s:1']},
                    silent | {'reply': 'x'*4001, 'reply_sentence_keys': ['s:1']},
                    silent | {'reply': 'x', 'reply_sentence_keys': ['s:1', 's:1']},
                    silent | {'control': 'pause', 'control_sentence_keys': ['s:1'], 'reply': 'x', 'reply_sentence_keys': ['s:1']}):
            with self.subTest(bad=bad), self.assertRaises(ModelError):
                Decision.parse(bad)

    def test_ephemeral_threads_interleaved_events_and_reuse(self):
        backend = self.backend()
        first = backend.decide({'synthetic': True}, 'Synthetic role')
        second = backend.decide({'synthetic': True}, 'Synthetic role')
        self.assertEqual(first, second)
        self.assertEqual(backend.last_model, 'synthetic-model')
        self.assertIsNone(backend.thread_id)
        self.assertFalse(any(path.name.startswith('rollout') for path in self.root.rglob('*')))

    def test_failures_are_sanitized_and_close_the_process(self):
        for mode in ('failed', 'invalid', 'malformed', 'tool','late-tool'):
            backend = self.backend(mode)
            with self.subTest(mode=mode), self.assertRaises(ModelError) as caught:
                backend.decide({}, 'Synthetic role')
            self.assertNotIn('private', str(caught.exception))
            self.assertIsNone(backend.process)

    def test_subscription_route_rejects_api_key_and_unavailable_tool_isolation(self):
        for mode,reason in [('apikey','model_subscription_required'),('exposed','model_tools_unavailable')]:
            backend=self.backend(mode)
            with self.assertRaises(ModelError) as caught:
                backend.decide({},'Synthetic role')
            self.assertEqual(caught.exception.reason,reason)
            self.assertIsNone(backend.process)
        self.assertIsNone(self.backend('inherited').decide({},'Synthetic role').reply)

    def test_stalled_stdin_write_respects_whole_decision_timeout(self):
        backend=self.backend('write-stall')
        started=time.monotonic()
        with self.assertRaises(ModelError) as caught:
            backend.decide({'body':'x'*100000},'Synthetic role')
        self.assertEqual(caught.exception.reason,'model_timeout')
        self.assertLess(time.monotonic()-started,2)
        self.assertEqual(backend.last_model,'synthetic-model')

    def test_timeout_and_user_stop_do_not_leave_a_child_running(self):
        for stop in (False, True):
            event = threading.Event()
            backend = self.backend('delay', stop_event=event)
            timer = threading.Timer(0.1, event.set) if stop else None
            if timer:
                timer.start()
            started = time.monotonic()
            with self.assertRaises(ModelError) as caught:
                backend.decide({}, 'Synthetic role')
            self.assertEqual(caught.exception.reason, 'model_stopped' if stop else 'model_timeout')
            self.assertLess(time.monotonic()-started, 2)
            self.assertIsNone(backend.process)
            if timer:
                timer.join()

    def test_startup_rpcs_share_one_decision_deadline(self):
        backend = self.backend('slow-start')
        started = time.monotonic()
        with self.assertRaises(ModelError) as caught:
            backend.decide({}, 'Synthetic role')
        self.assertEqual(caught.exception.reason, 'model_timeout')
        self.assertLess(time.monotonic() - started, 0.6)
        self.assertIsNone(backend.last_model)

    def test_packaged_role_and_local_override(self):
        text, version = load_role(self.settings)
        self.assertIn('任何参会者', text)
        self.assertEqual(len(version), 64)
        local = self.root / 'role.md'
        local.write_text('Synthetic role')
        self.assertEqual(load_role(replace(self.settings, role_file=local))[0], 'Synthetic role')
        link = self.root / 'link.md'
        link.symlink_to(local)
        with self.assertRaises(ModelError):
            load_role(replace(self.settings, role_file=link))

    def test_llm_config_preserves_legacy_defaults_and_validates_settings(self):
        path = self.root / 'config.yaml'
        base = 'app:\n  id: synthetic\n  secret: synthetic\nmeeting:\n  meeting_no: "012345678"\n'
        path.write_text(base)
        path.chmod(0o600)
        self.assertEqual(load_config(path).llm.backend, 'rule')
        path.write_text(base + 'llm:\n  backend: codex\n  role_file: role.md\n  introduction: false\n')
        settings = load_config(path).llm
        self.assertEqual(settings.role_file, self.root / 'role.md')
        self.assertFalse(settings.introduction)
        for bad in ('backend: unknown', 'timeout_seconds: 0', 'introduction: yesplease', 'extra: 1'):
            path.write_text(base + 'llm:\n  ' + bad + '\n')
            with self.assertRaises(ConfigError):
                load_config(path)
