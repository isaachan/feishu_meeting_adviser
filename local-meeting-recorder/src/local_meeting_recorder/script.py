"""Script parsing for the fake recorder: one JSON row per utterance."""
import json
from dataclasses import dataclass

MAX_TEXT_CHARACTERS = 2000
MAX_SPEAKER_CHARACTERS = 80
MAX_DELAY_MS = 3_600_000


@dataclass(frozen=True)
class ScriptLine:
    speaker: str | None
    text: str
    delay_ms: int


def parse_script(raw: str) -> list[ScriptLine]:
    """Parse a JSONL script: {"speaker": str|null, "text": str, "delay_ms": int>=0}."""
    lines: list[ScriptLine] = []
    for number, row in enumerate(raw.splitlines(), 1):
        if not row.strip():
            continue
        try:
            value = json.loads(row)
        except ValueError:
            raise ValueError(f'script line {number}: not valid JSON') from None
        if not isinstance(value, dict):
            raise ValueError(f'script line {number}: row must be a JSON object')
        text = value.get('text')
        speaker = value.get('speaker')
        delay = value.get('delay_ms', 0)
        if (not isinstance(text, str) or not text.strip()
                or len(text) > MAX_TEXT_CHARACTERS):
            raise ValueError(
                f'script line {number}: text must contain 1..{MAX_TEXT_CHARACTERS} characters')
        if speaker is not None and (not isinstance(speaker, str) or not speaker.strip()
                                    or len(speaker) > MAX_SPEAKER_CHARACTERS):
            raise ValueError(
                f'script line {number}: speaker must contain '
                f'1..{MAX_SPEAKER_CHARACTERS} characters or be null')
        if (isinstance(delay, bool) or not isinstance(delay, int)
                or not 0 <= delay <= MAX_DELAY_MS):
            raise ValueError(
                f'script line {number}: delay_ms must be an integer 0..{MAX_DELAY_MS}')
        lines.append(ScriptLine(speaker.strip() if speaker else None, text.strip(), delay))
    if not lines:
        raise ValueError('script has no utterances')
    return lines
