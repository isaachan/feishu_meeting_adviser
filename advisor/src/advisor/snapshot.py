"""Bounded model snapshot policy (ported from legacy advisor snapshot.py)."""
from dataclasses import asdict
from typing import Protocol

from .errors import ModelError, StorageError
from .models import Caption, epoch_ns
from .persistence import decode_mapping, encode
from .settings import AdviserSettings


class SnapshotData(Protocol):
    def session(self, session_id: str, /) -> dict | None: ...
    def context_rows(self, session_id: str, /, *, limit: int) -> list[dict]: ...
    def pending_reply_row(self, session_id: str, /) -> dict | None: ...
    def recent_reply_rows(self, session_id: str, /, *, limit: int) -> list[dict]: ...
    def context_count(self, session_id: str, /) -> int: ...
    def materials(self, session_id: str, /) -> dict | None: ...


def is_fresh_caption(caption: Caption, *, now: float, joined_at: float,
                     settings: AdviserSettings, window_seconds: int | None = None) -> bool:
    timestamp = caption.end_ns if caption.end_ns is not None else caption.event_ns
    if timestamp is None:
        return False
    seconds = timestamp / 1_000_000_000
    window = settings.freshness_seconds if window_seconds is None else window_seconds
    return joined_at <= seconds <= now + 5 and seconds >= now - window


def is_reply_evidence_caption(caption: Caption, *, now: float, joined_at: float,
                              settings: AdviserSettings) -> bool:
    return is_fresh_caption(caption, now=now, joined_at=joined_at,
                            settings=settings, window_seconds=settings.reply_evidence_seconds)


def _caption_from_json(raw) -> Caption:
    from .models import caption_from_json
    return caption_from_json(raw)


class SnapshotBuilder:
    def __init__(self, data: SnapshotData):
        self.data = data

    def build(self, session_id: str, settings: AdviserSettings, *, now: float,
              joined_at: float, role_characters: int = 0) -> dict:
        session = self.data.session(session_id)
        if not session:
            raise StorageError('unknown adviser session')
        rows = self.data.context_rows(session_id, limit=1000)
        captions, fresh_keys, reply_keys, fingerprints = [], [], [], {}
        used = 0
        for row in rows:
            caption = _caption_from_json(row['body'])
            item = asdict(caption) | {'conflicted': bool(row['suppressed'])}
            size = len(encode(item))
            if used + size > settings.context_characters:
                continue
            used += size
            captions.append(item)
            if not row['suppressed']:
                fingerprints[caption.sentence_key] = caption.fingerprint
                if is_reply_evidence_caption(caption, now=now, joined_at=joined_at,
                                             settings=settings):
                    reply_keys.append(caption.sentence_key)
                if (row['generation'] > session['decided_generation']
                        and is_fresh_caption(caption, now=now, joined_at=joined_at,
                                             settings=settings)):
                    fresh_keys.append(caption.sentence_key)

        def order(item):
            stamp = (epoch_ns(item['end_time'], numeric_milliseconds=True)
                     or epoch_ns(item['event_time']))
            return (stamp is None, stamp or 0, item['sentence_key'])

        captions.sort(key=order)
        pending = self.data.pending_reply_row(session_id)
        pending_reply = None
        if pending:
            basis = decode_mapping(pending['evidence'])
            pending_reply = {'uuid': pending['uuid'], 'text': pending['content'],
                             'reply_sentence_keys': list(basis)}
        sent = [{'text': row['content'], 'state': row['state']}
                for row in self.data.recent_reply_rows(session_id, limit=20)]
        result = {
            'generation': session['generation'],
            'captions': captions,
            'fresh_input_keys': fresh_keys,
            'evidence': fingerprints,
            'context_omitted': len(captions) < self.data.context_count(session_id),
            'pending_reply': pending_reply,
            'sent_replies': sent,
            'reply_allowed_keys': reply_keys,
        }
        material = self.data.materials(session_id)
        if material and material['state'] != 'disabled':
            # Old sessions may contain advisor-prefetched NSDP briefs; queries
            # now happen on demand and stale prefetches are not meeting facts.
            material['documents'] = [doc for doc in material['documents']
                                     if doc.get('kind') != 'nsdp']
            result['meeting_context'] = material

        while len(encode(result)) + role_characters > settings.context_characters:
            result['context_omitted'] = True
            if result['sent_replies']:
                result['sent_replies'].pop()
            elif result.get('meeting_context') and any(
                    item.get('text') for item in result['meeting_context']['documents']):
                item = max(result['meeting_context']['documents'],
                           key=lambda value: len(value.get('text', '')))
                excess = len(encode(result)) + role_characters - settings.context_characters
                item['text'] = item['text'][:-max(1, excess)]
                item['truncated'] = True
                result['meeting_context']['state'] = 'partial'
            elif result.get('meeting_context', {}).get('description'):
                context = result['meeting_context']
                excess = len(encode(result)) + role_characters - settings.context_characters
                context['description'] = context['description'][:-max(1, excess)]
                context['description_truncated'] = True
                context['state'] = 'partial'
            elif result['captions']:
                key = result['captions'].pop(0)['sentence_key']
                result['evidence'].pop(key, None)
                result['fresh_input_keys'] = [v for v in result['fresh_input_keys'] if v != key]
                result['reply_allowed_keys'] = [v for v in result['reply_allowed_keys'] if v != key]
            else:
                raise ModelError('model_context_budget')
        return result
