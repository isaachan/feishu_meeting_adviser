"""Rule-based stub backend: immediate decisions for the stub-first stages.

Not a product feature: used by the integration test bed and local dry runs
before the real gateway model is wired (fake-model-first acceptance method).
"""
from .model_backend import Decision


class RuleBackend:
    context_overhead_characters = 0

    def __init__(self, reply_text='（桩回答）我听到了。'):
        self.reply_text = reply_text
        self.metadata = {'backend': 'rule-stub'}

    def decide(self, snapshot, role):
        captions = [item for item in snapshot.get('captions', [])
                    if not item.get('conflicted')]
        fresh = snapshot.get('fresh_input_keys') or []
        if not fresh or not captions:
            return Decision('none', (), None, ())
        latest = captions[-1]
        text = self.reply_text.rstrip('。') + f"（{len(fresh)} 条新输入，最新：{latest['text'][:20]}）"
        return Decision('none', (), text, tuple(fresh))

    def diagnostic_metadata(self):
        return self.metadata

    def close(self):
        return None
