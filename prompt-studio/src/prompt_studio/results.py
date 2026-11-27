"""Read-only views of a replay run: session state, jobs/decisions, material."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import sqlite3
from pathlib import Path
from urllib.parse import quote

from .transcript import Transcript


class ResultError(RuntimeError):
    pass


def _connect(path: str | Path) -> sqlite3.Connection:
    target = Path(path)
    if not target.is_file():
        raise ResultError(f'advisor database not found: {target}')
    connection = sqlite3.connect(
        f'file:{quote(str(target.absolute()))}?mode=ro', uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute('PRAGMA busy_timeout=5000')
    return connection


def _json(raw, name: str):
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        raise ResultError(f'invalid {name} JSON') from None
    return value


@dataclass(frozen=True)
class SessionState:
    session_id: str
    meeting_no: str
    title: str
    phase: str
    generation: int
    decided_generation: int
    error: str | None
    retry_at: float
    material_state: str | None
    material_version: str | None

    def to_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass(frozen=True)
class Job:
    job_id: str
    generation: int
    state: str
    role_version: str
    created_ns: int
    context_version: str | None
    snapshot: dict | None
    decision: dict | None
    timings: dict | None

    @property
    def settled(self) -> bool:
        return self.state not in ('pending', 'running')

    def snapshot_summary(self) -> dict:
        snapshot = self.snapshot or {}
        return {
            'captions': len(snapshot.get('captions') or []),
            'fresh_input_keys': snapshot.get('fresh_input_keys') or [],
            'reply_allowed_keys': snapshot.get('reply_allowed_keys') or [],
            'pending_reply': bool(snapshot.get('pending_reply')),
            'meeting_context': bool(snapshot.get('meeting_context')),
            'context_omitted': bool(snapshot.get('context_omitted')),
        }

    def to_dict(self, *, include_snapshot: bool = False) -> dict:
        value = {
            'job_id': self.job_id, 'generation': self.generation,
            'state': self.state, 'role_version': self.role_version,
            'created_ns': self.created_ns,
            'context_version': self.context_version,
            'decision': self.decision, 'timings': self.timings,
            'snapshot_summary': self.snapshot_summary(),
        }
        if include_snapshot:
            value['snapshot'] = self.snapshot
        return value


@dataclass
class ReplayResult:
    run_id: str
    session_id: str
    meeting_no: str
    transcript: Transcript
    session_state: SessionState | None
    jobs: list[Job]
    records: list[dict]
    material: dict | None
    advisor_log: str | None = None

    @property
    def replies(self) -> list[str]:
        return [record['text'] for record in self.records
                if record.get('type') == 'llm']

    @property
    def complete_jobs(self) -> list[Job]:
        return [job for job in self.jobs if job.state == 'complete']

    def to_dict(self) -> dict:
        return {
            'run_id': self.run_id,
            'session_id': self.session_id,
            'meeting_no': self.meeting_no,
            'title': self.transcript.title,
            'description': self.transcript.description,
            'documents': self.transcript.documents,
            'turns': [turn.to_dict() for turn in self.transcript.turns],
            'session_state': self.session_state.to_dict() if self.session_state else None,
            'jobs': [job.to_dict() for job in self.jobs],
            'records': self.records,
            'material': self.material,
            'advisor_log': self.advisor_log,
        }


def read_session(db_path: str | Path, session_id: str) -> SessionState | None:
    connection = _connect(db_path)
    try:
        row = connection.execute(
            'SELECT session_id,meeting_no,title,phase,generation,'
            'decided_generation,error,retry_at,material_state,material_version '
            'FROM sessions WHERE session_id=?', (session_id,)).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    return SessionState(
        session_id=row['session_id'], meeting_no=row['meeting_no'],
        title=row['title'], phase=row['phase'],
        generation=row['generation'] or 0,
        decided_generation=row['decided_generation'] or 0,
        error=row['error'], retry_at=row['retry_at'] or 0.0,
        material_state=row['material_state'],
        material_version=row['material_version'])


def read_jobs(db_path: str | Path, session_id: str) -> list[Job]:
    connection = _connect(db_path)
    try:
        rows = connection.execute(
            'SELECT job_id,generation,state,role_version,created_ns,'
            'context_version,snapshot,result,timings FROM jobs '
            'WHERE session_id=? ORDER BY generation,created_ns', (session_id,)
        ).fetchall()
    finally:
        connection.close()
    return [Job(
        job_id=row['job_id'], generation=row['generation'], state=row['state'],
        role_version=row['role_version'], created_ns=row['created_ns'],
        context_version=row['context_version'],
        snapshot=_json(row['snapshot'], 'snapshot'),
        decision=_json(row['result'], 'result'),
        timings=_json(row['timings'], 'timings')) for row in rows]


def _epoch(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000.0 if abs(number) > 1e11 else number
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            number = float(text)
            return number / 1000.0 if len(text) > 10 else number
        try:
            return datetime.fromisoformat(text.replace('Z', '+00:00')).timestamp()
        except ValueError:
            return None
    return None


def read_records(db_path: str | Path, session_id: str) -> list[dict]:
    connection = _connect(db_path)
    try:
        captions = connection.execute(
            'SELECT body FROM context WHERE session_id=? ORDER BY generation',
            (session_id,)).fetchall()
        replies = connection.execute(
            "SELECT content,created_ns FROM outbox WHERE session_id=? "
            "AND kind='reply' AND channel='local' ORDER BY created_ns",
            (session_id,)).fetchall()
    finally:
        connection.close()
    records = []
    for row in captions:
        body = _json(row['body'], 'caption') or {}
        stamp = (_epoch(body.get('end_time')) or _epoch(body.get('event_time'))
                 or _epoch(body.get('received_at')))
        records.append({'type': 'human', 'time': stamp,
                        'speaker': body.get('speaker_name') or body.get('speaker_id'),
                        'text': body.get('text') or ''})
    for row in replies:
        records.append({'type': 'llm', 'time': row['created_ns'] / 1e9,
                        'speaker': 'LLM', 'text': row['content']})
    records.sort(key=lambda record: (record['time'] is None, record['time'] or 0.0))
    return records


def read_material(db_path: str | Path, session_id: str) -> dict | None:
    connection = _connect(db_path)
    try:
        row = connection.execute(
            'SELECT body FROM session_materials WHERE session_id=?',
            (session_id,)).fetchone()
    finally:
        connection.close()
    if row is None:
        return None
    body = _json(row['body'], 'material')
    return body if isinstance(body, dict) else None
