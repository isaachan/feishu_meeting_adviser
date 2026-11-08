"""Apply a model decision after revalidating evidence against current context."""
from typing import Any, Protocol
import time
from uuid import NAMESPACE_URL, uuid5

from .llm import ModelError
from .models import JobState, ParticipationState, digest
from .snapshot import is_fresh_caption, is_reply_evidence_caption
from .storage import caption_from_json, decode_mapping, encode


class DecisionData(Protocol):
    def context_row(self, meeting_id: str, sentence_key: str, /) -> dict | None: ...
    def unit_of_work(self, *, fault=None) -> Any: ...


class DecisionApplier:
    def __init__(self, data: DecisionData):
        self.data = data

    def apply(self, job: dict, decision, *, now: float | None = None, settings=None,
              joined_at: float = 0, bot_id: str | None = None,
              notifications=None, fault=None) -> bool:
        now = time.time() if now is None else now
        snapshot = decode_mapping(job['snapshot'])
        fresh_keys = set(snapshot['fresh_input_keys'])
        reply_keys = set(snapshot.get('reply_allowed_keys', snapshot['fresh_input_keys']))
        if (not set(decision.control_sentence_keys) <= fresh_keys
                or not set(decision.reply_sentence_keys) <= reply_keys):
            raise ModelError('model_invalid_evidence')

        valid_control, valid_reply = {}, {}
        for key in set(decision.control_sentence_keys) | set(decision.reply_sentence_keys):
            row = self.data.context_row(job['meeting_id'], key)
            if row and not row['suppressed']:
                caption = caption_from_json(row['body'])
                if caption.fingerprint == snapshot['evidence'].get(key):
                    if settings is None or is_fresh_caption(
                            caption, now=now, joined_at=joined_at,
                            settings=settings, bot_id=bot_id):
                        valid_control[key] = caption.fingerprint
                    if settings is None or is_reply_evidence_caption(
                            caption, now=now, joined_at=joined_at,
                            settings=settings, bot_id=bot_id):
                        valid_reply[key] = caption.fingerprint
        control_valid = all(key in valid_control for key in decision.control_sentence_keys)
        reply_valid = decision.reply is not None and all(
            key in valid_reply for key in decision.reply_sentence_keys)

        with self.data.unit_of_work(fault=fault) as tx:
            state = tx.jobs.decision_state(job['job_id'])
            if state in (JobState.COMPLETE, JobState.ABANDONED):
                return False
            meeting = tx.meetings.require(job['meeting_id'])
            if (job.get('context_version') != meeting['material_version']
                    or meeting['material_state'] == 'unavailable'):
                tx.jobs.abandon(job['job_id'])
                return False
            participation = ParticipationState(meeting['participation'])
            if control_valid:
                if decision.control == 'pause':
                    participation = ParticipationState.PAUSED
                elif decision.control == 'resume':
                    participation = ParticipationState.ACTIVE
            tx.outbox.cancel_pending(job['meeting_id'])
            if (reply_valid and participation == ParticipationState.ACTIVE
                    and not (control_valid and decision.control in ('pause', 'cancel'))):
                basis = {key: valid_reply[key] for key in decision.reply_sentence_keys}
                message_uuid = str(uuid5(NAMESPACE_URL, digest([
                    'adviser-reply-v1', job['meeting_id'], job['role_version'], basis, decision.reply])))
                tx.outbox.save_reply(
                    message_uuid=message_uuid,
                    meeting_id=job['meeting_id'],
                    content=decision.reply,
                    evidence=encode(basis),
                    generation=job['generation'],
                    created_ns=int(now * 1_000_000_000),
                    context_version=meeting['material_version'],
                    meeting_comment=notifications.meeting_comment if notifications else True,
                    recipients=notifications.recipients if notifications else (),
                )
            brief = {
                'generation': job['generation'],
                'snapshot_hash': digest(snapshot),
                'context_version': meeting['material_version'],
                'evidence': {
                    key: snapshot['evidence'].get(key)
                    for key in set(decision.control_sentence_keys) | set(decision.reply_sentence_keys)
                },
            }
            tx.jobs.complete(
                job['job_id'], result=encode(decision.to_dict()), snapshot=encode(brief))
            tx.meetings.complete_decision(
                job['meeting_id'], generation=job['generation'], participation=participation)
        return True
