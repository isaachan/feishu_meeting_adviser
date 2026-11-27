"""Prompt text for the LLM decision backend, loaded from plain-text files.

The system/agent wording, the snapshot rules and the decision-v2 reply
formatting rules live as UTF-8 text files so they can be edited and versioned
independently of the execution code.

The default directory is the packaged ``prompts/`` folder. ``load_prompts``
also accepts an explicit directory so a local replay harness can switch prompt
sets without reinstalling the package; callers that need per-decision reloads
call the individual loaders each time.
"""
import json
from dataclasses import dataclass
from pathlib import Path

from ..decisions import DECISION_SCHEMA

DEFAULT_PROMPT_DIR = Path(__file__).parent

SCHEMA_PREFIX = '响应必须符合以下 JSON schema:'


def _load(directory: Path, name: str) -> str:
    return (directory / name).read_text(encoding='utf-8').rstrip('\n')


def load_agent_prompt(directory: Path | None = None) -> str:
    base = Path(directory) if directory is not None else DEFAULT_PROMPT_DIR
    return (_load(base, 'agent_prompt.txt')
            + _load(base, 'snapshot_rules.txt')
            + SCHEMA_PREFIX + json.dumps(DECISION_SCHEMA))


def load_reply_markdown_rules(directory: Path | None = None) -> str:
    base = Path(directory) if directory is not None else DEFAULT_PROMPT_DIR
    return _load(base, 'reply_markdown_rules.txt')


@dataclass(frozen=True)
class PromptSet:
    agent_prompt: str
    reply_markdown_rules: str


def load_prompts(directory: Path | None = None) -> PromptSet:
    return PromptSet(agent_prompt=load_agent_prompt(directory),
                     reply_markdown_rules=load_reply_markdown_rules(directory))


SNAPSHOT_RULES = _load(DEFAULT_PROMPT_DIR, 'snapshot_rules.txt')
AGENT_PROMPT = load_agent_prompt()
REPLY_MARKDOWN_RULES = load_reply_markdown_rules()
