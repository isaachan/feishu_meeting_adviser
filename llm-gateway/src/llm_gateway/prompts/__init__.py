"""Prompt text for the LLM decision backend, loaded from plain-text files.

The system/agent wording, the snapshot rules and the decision-v2 reply
formatting rules live as UTF-8 text files so they can be edited and versioned
independently of the execution code.
"""
import json
from pathlib import Path

from ..decisions import DECISION_SCHEMA

_PROMPT_DIR = Path(__file__).parent


def _load(name: str) -> str:
    return (_PROMPT_DIR / name).read_text(encoding='utf-8').rstrip('\n')


SNAPSHOT_RULES = _load('snapshot_rules.txt')
AGENT_PROMPT = (_load('agent_prompt.txt') + SNAPSHOT_RULES
                + '响应必须符合以下 JSON schema:' + json.dumps(DECISION_SCHEMA))
REPLY_MARKDOWN_RULES = _load('reply_markdown_rules.txt')
