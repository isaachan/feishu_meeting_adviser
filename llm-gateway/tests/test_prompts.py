import json
import tempfile
import unittest
from pathlib import Path

from llm_gateway.decisions import DECISION_SCHEMA
from llm_gateway.prompts import (AGENT_PROMPT, DEFAULT_PROMPT_DIR,
                                 REPLY_MARKDOWN_RULES, SCHEMA_PREFIX,
                                 load_agent_prompt, load_prompts,
                                 load_reply_markdown_rules)


def packaged(name: str) -> str:
    return (DEFAULT_PROMPT_DIR / name).read_text(encoding='utf-8').rstrip('\n')


class PromptLoadingTests(unittest.TestCase):
    def test_default_prompts_match_packaged_files(self):
        self.assertEqual(load_agent_prompt(), AGENT_PROMPT)
        self.assertEqual(load_reply_markdown_rules(), REPLY_MARKDOWN_RULES)
        self.assertIn(packaged('agent_prompt.txt'), AGENT_PROMPT)
        self.assertIn(packaged('snapshot_rules.txt'), AGENT_PROMPT)
        self.assertIn(SCHEMA_PREFIX + json.dumps(DECISION_SCHEMA), AGENT_PROMPT)

    def test_custom_directory_is_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            (base / 'agent_prompt.txt').write_text('CUSTOM AGENT\n')
            (base / 'snapshot_rules.txt').write_text('CUSTOM SNAPSHOT\n')
            (base / 'reply_markdown_rules.txt').write_text('CUSTOM REPLY\n')
            prompts = load_prompts(base)
            self.assertEqual(prompts.agent_prompt,
                             'CUSTOM AGENT' + 'CUSTOM SNAPSHOT'
                             + SCHEMA_PREFIX + json.dumps(DECISION_SCHEMA))
            self.assertEqual(prompts.reply_markdown_rules, 'CUSTOM REPLY')

    def test_missing_files_raise_oserror(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            with self.assertRaises(OSError):
                load_agent_prompt(base)
            with self.assertRaises(OSError):
                load_reply_markdown_rules(base)
