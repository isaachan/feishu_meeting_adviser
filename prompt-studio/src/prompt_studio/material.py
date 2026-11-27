"""Patch an advisor session's meeting material before captions are replayed.

The advisor stores the calendar material in one ``session_materials`` row plus
``sessions.material_version`` / ``sessions.material_state``. Snapshot building
reads that row on every decision, so writing it after session creation (and
before the first caption) is equivalent to the calendar providing this
description and document set. No advisor code or schema change is involved.

The version string only has to be internally consistent: job rows copy
``sessions.material_version`` and the decision applier compares equality. We use
a stable SHA-256 over the injected material.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from typing import Iterable
from urllib.parse import quote


class MaterialPatchError(RuntimeError):
    pass


def _document(value: dict) -> dict:
    kind = value.get('kind') or 'doc'
    title = value.get('title') or ''
    text = value.get('text') or ''
    if not all(isinstance(item, str) for item in (kind, title, text)):
        raise MaterialPatchError('document kind/title/text must be strings')
    return {'kind': kind, 'title': title, 'text': text}


def material_body(description: str, documents: Iterable[dict] = ()) -> dict:
    if not isinstance(description, str):
        raise MaterialPatchError('description must be a string')
    docs = [_document(item) for item in documents]
    has_content = bool(description.strip()) or any(item['text'].strip() for item in docs)
    version = None
    if has_content:
        identity = ['material-v1', description, docs]
        version = hashlib.sha256(
            json.dumps(identity, ensure_ascii=False, separators=(',', ':'))
            .encode('utf-8')).hexdigest()
    return {
        'state': 'ready' if has_content else 'disabled',
        'context_version': version,
        'description': description,
        'documents': docs,
    }


def patch_material(db_path: str | Path, session_id: str, description: str = '',
                   documents: Iterable[dict] = (), *, now: float | None = None) -> dict:
    """Apply the material in one immediate transaction; returns the body."""
    path = Path(db_path)
    if not path.is_file():
        raise MaterialPatchError(f'advisor database not found: {path}')
    body = material_body(description, documents)
    loaded_ns = int((time.time() if now is None else now) * 1_000_000_000)
    connection = sqlite3.connect(
        f'file:{quote(str(path.absolute()))}?mode=rw', uri=True,
        timeout=5, isolation_level=None)
    try:
        connection.execute('PRAGMA busy_timeout=5000')
        connection.execute('BEGIN IMMEDIATE')
        changed = connection.execute(
            'UPDATE sessions SET material_version=?,material_state=? '
            'WHERE session_id=?',
            (body['context_version'], body['state'], session_id)).rowcount
        if changed != 1:
            raise MaterialPatchError(f'unknown session: {session_id}')
        changed = connection.execute(
            'UPDATE session_materials SET body=?,loaded_ns=? WHERE session_id=?',
            (json.dumps(body, ensure_ascii=False), loaded_ns, session_id)).rowcount
        if changed != 1:
            raise MaterialPatchError(f'session has no material row: {session_id}')
        connection.execute('COMMIT')
    except BaseException:
        try:
            connection.execute('ROLLBACK')
        except sqlite3.Error:
            pass
        raise
    finally:
        connection.close()
    return body
