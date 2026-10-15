"""Bounded model snapshot policy, independent of SQLite."""
from dataclasses import asdict
from typing import Protocol

from .llm import ModelError
from .models import Caption, epoch_ns
from .storage import StorageError, caption_from_json, decode_mapping, encode


class SnapshotData(Protocol):
    def meeting(self, meeting_id: str) -> dict | None: ...
    def context_rows(self, meeting_id: str, *, limit: int) -> list[dict]: ...
    def pending_reply_row(self, meeting_id: str) -> dict | None: ...
    def recent_reply_rows(self, meeting_id: str, *, limit: int) -> list[dict]: ...
    def context_count(self, meeting_id: str) -> int: ...
    def materials(self, meeting_id: str) -> dict | None: ...


def is_fresh_caption(caption: Caption, *, now: float, joined_at: float, settings,
                     bot_id: str | None = None) -> bool:
    timestamp = caption.end_ns if caption.end_ns is not None else caption.event_ns
    if timestamp is None or caption.speaker_id == bot_id and bot_id is not None:
        return False
    seconds = timestamp / 1_000_000_000
    return joined_at <= seconds <= now + 5 and seconds >= now - settings.freshness_seconds


class SnapshotBuilder:
    def __init__(self, data: SnapshotData):
        self.data = data

    def build(self, meeting_id: str, settings, *, now: float, joined_at: float,
              bot_id: str | None = None, role_characters: int = 0) -> dict:
        meeting = self.data.meeting(meeting_id)
        if not meeting:
            raise StorageError('unknown adviser meeting')
        rows = self.data.context_rows(meeting_id, limit=1000)
        captions, fresh_keys, fingerprints = [], [], {}
        used = 0
        for row in rows:
            caption = caption_from_json(row['body'])
            item = asdict(caption) | {'conflicted': bool(row['suppressed'])}
            size = len(encode(item))
            if used + size > settings.context_characters:
                continue
            used += size
            captions.append(item)
            if not row['suppressed']:
                fingerprints[caption.sentence_key] = caption.fingerprint
                if (row['generation'] > meeting['decided_generation'] and is_fresh_caption(
                        caption, now=now, joined_at=joined_at, settings=settings, bot_id=bot_id)):
                    fresh_keys.append(caption.sentence_key)

        def order(item):
            stamp = epoch_ns(item['end_time'], numeric_milliseconds=True) or epoch_ns(item['event_time'])
            return (stamp is None, stamp or 0, item['sentence_key'])

        captions.sort(key=order)
        pending = self.data.pending_reply_row(meeting_id)
        pending_reply = None
        reply_keys = list(fresh_keys)
        if pending:
            basis = decode_mapping(pending['evidence'])
            pending_reply = {
                'uuid': pending['uuid'],
                'text': pending['content'],
                'reply_sentence_keys': list(basis),
            }
            for key, fingerprint in basis.items():
                row = next((item for item in captions if item['sentence_key'] == key), None)
                if row and fingerprints.get(key) == fingerprint:
                    caption = caption_from_json(encode({k: v for k, v in row.items() if k != 'conflicted'}))
                    if is_fresh_caption(
                            caption, now=now, joined_at=joined_at, settings=settings, bot_id=bot_id):
                        reply_keys.append(key)
        sent = [
            {'text': row['content'], 'state': row['state']}
            for row in self.data.recent_reply_rows(meeting_id, limit=20)
        ]
        result = {
            'generation': meeting['generation'],
            'participation': meeting['participation'],
            'source_state': meeting['source_state'],
            'captions': captions,
            'source_status': decode_mapping(meeting['source_status']) if meeting['source_status'] else None,
            'fresh_input_keys': fresh_keys,
            'evidence': fingerprints,
            'context_omitted': len(captions) < self.data.context_count(meeting_id),
            'pending_reply': pending_reply,
            'sent_replies': sent,
            'reply_allowed_keys': list(set(reply_keys)),
        }
        material = self.data.materials(meeting_id)
        if material and material['state'] != 'disabled':
            result['meeting_context'] = material

        # Count the entire serialized snapshot plus role. Prefer captions to old
        # replies and material text, while preserving source metadata.
        while len(encode(result)) + role_characters > settings.context_characters:
            result['context_omitted'] = True
            if result['sent_replies']:
                result['sent_replies'].pop()
            elif result.get('meeting_context') and any(
                    item.get('text') for item in result['meeting_context']['documents']):
                item = max(
                    result['meeting_context']['documents'], key=lambda value: len(value.get('text', '')))
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
                result['fresh_input_keys'] = [value for value in result['fresh_input_keys'] if value != key]
                result['reply_allowed_keys'] = [
                    value for value in result['reply_allowed_keys'] if value != key]
            else:
                raise ModelError('model_context_budget')
        return result
