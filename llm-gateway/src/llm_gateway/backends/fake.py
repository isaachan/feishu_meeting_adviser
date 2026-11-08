"""Deterministic fake execution used by M1 and the gateway test suite.

Behaviour is selected through ``metadata.fake`` (never through the payload):

    {"fake": {"behavior": "ok"|"fresh"|"silence"|"slow"|"timeout"|"error"|"invalid",
              "reply": "...", "keys": ["..."], "seconds": 2.0}}
"""
import threading
import time

from .base import BackendError, ModelBackend


class FakeBackend(ModelBackend):
    def decide(self, system, input_value, *, deadline_seconds, stop_event, metadata):
        fake = metadata.get('fake') if isinstance(metadata, dict) else None
        fake = fake if isinstance(fake, dict) else {}
        behavior = fake.get('behavior', 'silence')
        if behavior == 'slow':
            if stop_event.wait(float(fake.get('seconds', 1.0))):
                raise BackendError('cancelled')
        if behavior == 'timeout':
            # Overrun the whole deadline; the service records execution_timeout.
            stop_event.wait(deadline_seconds + 5.0)
            raise BackendError('execution_timeout')
        if behavior == 'error':
            raise BackendError('backend_unavailable')
        if behavior == 'invalid':
            return {'not': 'a decision'}
        if behavior == 'ok':
            reply = fake.get('reply')
            keys = fake.get('keys', [])
            if not isinstance(reply, str) or not isinstance(keys, list):
                raise BackendError('backend_unavailable')
            return {'control': 'none', 'control_sentence_keys': [],
                    'reply': reply, 'reply_sentence_keys': keys}
        if behavior == 'fresh':
            keys = input_value.get('fresh_input_keys') if isinstance(input_value, dict) else None
            if isinstance(keys, list) and keys:
                return {'control': 'none', 'control_sentence_keys': [],
                        'reply': str(fake.get('reply', '网关合成回答')),
                        'reply_sentence_keys': [keys[0]]}
            return {'control': 'none', 'control_sentence_keys': [], 'reply': None,
                    'reply_sentence_keys': []}
        return {'control': 'none', 'control_sentence_keys': [], 'reply': None,
                'reply_sentence_keys': []}


class SleepingBackend(ModelBackend):
    """Test helper: blocks until released or stopped, then reports elapsed time."""

    def __init__(self):
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def decide(self, system, input_value, *, deadline_seconds, stop_event, metadata):
        self.calls += 1
        self.entered.set()
        started = time.monotonic()
        while not self.release.wait(0.02):
            if stop_event.is_set():
                raise BackendError('cancelled')
        self.elapsed = time.monotonic() - started
        return {'control': 'none', 'control_sentence_keys': [], 'reply': None,
                'reply_sentence_keys': []}
