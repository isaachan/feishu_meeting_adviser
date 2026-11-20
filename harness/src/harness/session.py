"""Simulated meeting state and snapshot construction.

Mirrors the adviser store/snapshot semantics closely enough for LLM behavior
testing: per-caption generation bump, fresh window 240s, reply-evidence window
600s, byte-budgeted caption context (newest kept, oldest dropped), and
decided_generation catch-up after an applied decision.
"""
from datetime import datetime, timezone
import hashlib
import json
import time

FRESHNESS_SECONDS = 240
REPLY_EVIDENCE_SECONDS = 600
CONTEXT_CHARACTERS = 30000
SENT_REPLIES_LIMIT = 20
LOCAL_SPEAKER = '本机'
MEETING_ID = 'harness'


def _iso(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).isoformat(timespec='milliseconds')


def caption_fingerprint(caption: dict) -> str:
    identity = [caption['text'], caption['speaker_id'], caption['speaker_name'],
                caption['language'], caption['start_time'], caption['end_time']]
    return hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode('utf-8')).hexdigest()


def _window_fresh(end_time_ms: int, *, now: float, joined_at: float, window: int) -> bool:
    seconds = end_time_ms / 1000
    return (int(joined_at * 1000) <= end_time_ms
            and seconds <= now + 5 and seconds >= now - window)


class MeetingSession:
    def __init__(self, *, freshness_seconds: int = FRESHNESS_SECONDS,
                 reply_evidence_seconds: int = REPLY_EVIDENCE_SECONDS,
                 context_characters: int = CONTEXT_CHARACTERS, clock=None):
        self._clock = clock or time.time
        self.freshness_seconds = freshness_seconds
        self.reply_evidence_seconds = reply_evidence_seconds
        self.context_characters = context_characters
        self.started_at = self._clock()
        self.captions: list[dict] = []
        self.sent_replies: list[dict] = []
        self.generation = 0
        self.decided_generation = 0

    @property
    def pending_count(self) -> int:
        return max(0, self.generation - self.decided_generation)

    def add_caption(self, text: str, speaker_name: str | None = None, *,
                    now: float | None = None, duration_ms: int | None = None) -> str:
        moment = self._clock() if now is None else now
        if duration_ms is None:
            duration_ms = max(1000, min(len(text) * 200, 30000))
        now_ms = int(moment * 1000)
        self.generation += 1
        key = f's{self.generation:04d}'
        self.captions.append({
            'meeting_id': MEETING_ID, 'sentence_key': key, 'text': text,
            'received_at': _iso(moment), 'sentence_id': None, 'speaker_id': None,
            'speaker_name': speaker_name, 'language': None, 'event_id': None,
            'event_time': now_ms, 'start_time': now_ms - duration_ms,
            'end_time': now_ms, 'conflicted': False,
        })
        return key

    def snapshot(self, *, now: float | None = None) -> dict:
        moment = self._clock() if now is None else now
        captions: list[dict] = []
        fresh_keys: list[str] = []
        fingerprints: dict[str, str] = {}
        reply_keys: list[str] = []
        used = 0
        for index in range(len(self.captions) - 1, -1, -1):
            caption = self.captions[index]
            size = len(json.dumps(caption, ensure_ascii=False).encode('utf-8'))
            if used + size > self.context_characters:
                continue
            used += size
            captions.append(caption)
            key = caption['sentence_key']
            fingerprints[key] = caption_fingerprint(caption)
            if _window_fresh(caption['end_time'], now=moment, joined_at=self.started_at,
                             window=self.reply_evidence_seconds):
                reply_keys.append(key)
            if (index + 1) > self.decided_generation and _window_fresh(
                    caption['end_time'], now=moment, joined_at=self.started_at,
                    window=self.freshness_seconds):
                fresh_keys.append(key)
        captions.sort(key=lambda item: (item['end_time'], item['sentence_key']))
        fresh_keys.reverse()
        reply_keys.reverse()
        return {
            'generation': self.generation,
            'captions': captions,
            'fresh_input_keys': fresh_keys,
            'evidence': fingerprints,
            'context_omitted': len(captions) < len(self.captions),
            'pending_reply': None,
            'sent_replies': [dict(item) for item in self.sent_replies[-SENT_REPLIES_LIMIT:]],
            'reply_allowed_keys': reply_keys,
        }

    def apply_decision(self, decision: dict) -> None:
        self.decided_generation = max(self.decided_generation, self.generation)
        if decision.get('reply'):
            self.sent_replies.append({'text': decision['reply'], 'state': 'sent'})
