"""Admin-site TXT transcript parsing and the editable replay scene model.

The exported TXT is a human-readable rendering of advisor ``records``:

    会参谋 · 文字记录
    会话 s-... · 会议 123456789 · 周会
    导出时间 2026/11/26 16:00:00 · 共 3 条

    [2026/11/26 16:02:03] 本机: 我们过一下预算
    [时间未知] LLM: 预算结论是……
    [2026/11/26 16:02:10] 未识别: 我补充一点

Only ``human`` turns are replayed; ``LLM`` rows are kept as the historical
comparison column. Timestamps are display-only: replay uses the advisor's wall
clock, so the original absolute times are not reproduced.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import json
import re

HEADER_MARK = '会参谋 · 文字记录'
RECORD = re.compile(r'^\[(?P<time>[^\]]*)\]\s?(?P<rest>.*)$')
SESSION_PART = re.compile(r'^会话\s+(?P<value>\S+)\s*$')
MEETING_PART = re.compile(r'^会议\s+(?P<value>\S+)\s*$')
UNKNOWN_TIME = {'', '时间未知', '未知', '时间未知 '}


class TranscriptError(ValueError):
    def __init__(self, message: str, *, line: int | None = None):
        self.line = line
        super().__init__(f'line {line}: {message}' if line is not None else message)


@dataclass(frozen=True)
class Turn:
    kind: str  # human | llm
    speaker: str | None
    text: str
    time_text: str | None = None

    def to_dict(self) -> dict:
        return {'kind': self.kind, 'speaker': self.speaker,
                'text': self.text, 'time_text': self.time_text}

    @classmethod
    def from_dict(cls, value: object) -> 'Turn':
        if not isinstance(value, dict):
            raise TranscriptError('turn must be an object')
        kind = value.get('kind')
        text = value.get('text')
        speaker = value.get('speaker')
        time_text = value.get('time_text')
        if kind not in ('human', 'llm'):
            raise TranscriptError('turn kind must be human or llm')
        if not isinstance(text, str):
            raise TranscriptError('turn text must be a string')
        if speaker is not None and not isinstance(speaker, str):
            raise TranscriptError('turn speaker must be a string or null')
        if time_text is not None and not isinstance(time_text, str):
            raise TranscriptError('turn time_text must be a string or null')
        return cls(kind=kind, speaker=speaker, text=text, time_text=time_text)


@dataclass
class Transcript:
    session_id: str | None = None
    meeting_no: str | None = None
    title: str | None = None
    description: str = ''
    documents: list[dict] = field(default_factory=list)
    turns: list[Turn] = field(default_factory=list)

    @property
    def human_turns(self) -> list[Turn]:
        return [turn for turn in self.turns if turn.kind == 'human']

    def validate(self) -> None:
        if not self.human_turns:
            raise TranscriptError('transcript needs at least one human turn')
        for index, turn in enumerate(self.turns):
            if not turn.text.strip():
                raise TranscriptError(f'turn {index} is empty')

    def to_dict(self) -> dict:
        return {
            'session_id': self.session_id,
            'meeting_no': self.meeting_no,
            'title': self.title,
            'description': self.description,
            'documents': [dict(item) for item in self.documents],
            'turns': [turn.to_dict() for turn in self.turns],
        }

    @classmethod
    def from_dict(cls, value: object) -> 'Transcript':
        if not isinstance(value, dict):
            raise TranscriptError('scene must be an object')
        turns_value = value.get('turns')
        if not isinstance(turns_value, list):
            raise TranscriptError('scene turns must be a list')
        documents = value.get('documents') or []
        if not isinstance(documents, list) or any(not isinstance(item, dict)
                                                 for item in documents):
            raise TranscriptError('scene documents must be a list of objects')
        return cls(
            session_id=_optional_str(value.get('session_id'), 'session_id'),
            meeting_no=_optional_str(value.get('meeting_no'), 'meeting_no'),
            title=_optional_str(value.get('title'), 'title'),
            description=_optional_str(value.get('description'), 'description') or '',
            documents=[dict(item) for item in documents],
            turns=[Turn.from_dict(item) for item in turns_value],
        )


def _optional_str(value: object, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TranscriptError(f'{name} must be a string or null')
    return value


def parse_text(text: str) -> Transcript:
    """Parse the admin-site TXT export. Tolerant of missing header/metadata."""
    lines = text.splitlines()
    first_record = next((index for index, line in enumerate(lines)
                         if RECORD.match(line)), None)
    if first_record is not None:
        start = first_record
    else:
        start = len(lines)

    transcript = Transcript()
    for line in lines[:start]:
        for part in line.split(' · '):
            match = SESSION_PART.fullmatch(part.strip())
            if match:
                transcript.session_id = match.group('value')
                continue
            match = MEETING_PART.fullmatch(part.strip())
            if match:
                transcript.meeting_no = match.group('value')
                continue
            if ('会话 ' in line) and part.strip() and transcript.title is None:
                transcript.title = part.strip()

    turns: list[Turn] = []
    for offset, line in enumerate(lines[start:], start=start + 1):
        if not line.strip():
            continue
        match = RECORD.match(line)
        if match:
            turns.append(_record(match.group('time'), match.group('rest'), offset))
        elif turns:
            turns[-1] = replace(turns[-1], text=turns[-1].text + '\n' + line)
    transcript.turns = turns
    return transcript


def _record(time_text: str, rest: str, line: int) -> Turn:
    speaker, sep, body = rest.partition(': ')
    if not sep:
        speaker, body = rest, ''
    speaker = speaker.strip()
    kind = 'llm' if speaker == 'LLM' else 'human'
    if speaker in ('LLM', '未识别', ''):
        speaker_value = 'LLM' if kind == 'llm' else None
    else:
        speaker_value = speaker
    time_value = time_text.strip()
    if time_value in UNKNOWN_TIME:
        time_value = None
    if not body.strip() and kind == 'human':
        raise TranscriptError('empty human turn', line=line)
    return Turn(kind=kind, speaker=speaker_value, text=body,
                time_text=time_value)


def render_text(transcript: Transcript) -> str:
    """Render back to the admin TXT shape (for the editor preview/download)."""
    title = transcript.title or '无标题'
    meeting = transcript.meeting_no or '—'
    head = [
        HEADER_MARK,
        f'会话 {transcript.session_id or "—"} · 会议 {meeting} · {title}',
        f'导出时间 — · 共 {len(transcript.turns)} 条',
        '',
    ]
    rows = []
    for turn in transcript.turns:
        speaker = turn.speaker or ('LLM' if turn.kind == 'llm' else '未识别')
        stamp = turn.time_text or '时间未知'
        rows.append(f'[{stamp}] {speaker}: {turn.text}')
    return '\n'.join(head + rows) + '\n'


def scene_json(transcript: Transcript) -> str:
    return json.dumps(transcript.to_dict(), ensure_ascii=False, indent=2)
