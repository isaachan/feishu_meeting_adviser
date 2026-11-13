"""Apply a model decision after revalidating evidence (ported; no pause/resume).

Plan C deltas vs legacy: control is none|cancel only (no pause participation);
sessions carry phase — ended sessions never apply late results (STATE_MACHINES);
fan-out recipients = owner + approved subscriptions, queried inside the same
unit of work as the decision (仅前向：集合以决策落库时刻为准).
"""
from typing import Any, Protocol
import json
import logging
import time
from uuid import NAMESPACE_URL, uuid5

from .errors import ModelError
from .models import JobState, caption_from_json, digest
from .snapshot import is_fresh_caption, is_reply_evidence_caption
from .persistence import encode

LOG = logging.getLogger(__name__)


class DecisionData(Protocol):
    def context_row(self, session_id: str, sentence_key: str, /) -> dict | None: ...
    def unit_of_work(self, *, fault=None) -> Any: ...
    def session(self, session_id: str, /) -> dict | None: ...


class DecisionApplier:
    def __init__(self, data: DecisionData):
        self.data = data

    def apply(self, job: dict, decision, *, now: float | None = None, settings=None,
              fault=None) -> bool:
        now = time.time() if now is None else now
        session_for_freshness = self.data.session(job['session_id'])
        joined_at = session_for_freshness['started_at'] if session_for_freshness else 0.0
        snapshot = json.loads(job['snapshot'])
        fresh_keys = set(snapshot['fresh_input_keys'])
        reply_keys = set(snapshot.get('reply_allowed_keys', snapshot['fresh_input_keys']))
        if (not set(decision.control_sentence_keys) <= fresh_keys
                or not set(decision.reply_sentence_keys) <= reply_keys):
            raise ModelError('model_invalid_evidence')

        valid_control, valid_reply = {}, {}
        for key in set(decision.control_sentence_keys) | set(decision.reply_sentence_keys):
            row = self.data.context_row(job['session_id'], key)
            if row and not row['suppressed']:
                caption = caption_from_json(row['body'])
                if caption.fingerprint == snapshot['evidence'].get(key):
                    if settings is None or is_fresh_caption(
                            caption, now=now, joined_at=joined_at, settings=settings):
                        valid_control[key] = caption.fingerprint
                    if settings is None or is_reply_evidence_caption(
                            caption, now=now, joined_at=joined_at, settings=settings):
                        valid_reply[key] = caption.fingerprint
        control_valid = all(key in valid_control for key in decision.control_sentence_keys)
        reply_valid = decision.reply is not None and all(
            key in valid_reply for key in decision.reply_sentence_keys)

        with self.data.unit_of_work(fault=fault) as tx:
            state = tx.jobs.decision_state(job['job_id'])
            if state in (JobState.COMPLETE, JobState.ABANDONED):
                LOG.info('decision replay ignored: session=%s job=%s state=%s',
                         job['session_id'], job['job_id'][:12], state.value)
                return False  # 决策幂等：重放不重复落库
            session = tx.sessions.require(job['session_id'])
            if session['phase'] == 'ended':
                tx.jobs.abandon(job['job_id'])
                LOG.info('decision abandoned (session ended): session=%s job=%s',
                         job['session_id'], job['job_id'][:12])
                return False  # ended 拒绝迟到结果
            if (job.get('context_version') != session['material_version']):
                tx.jobs.abandon(job['job_id'])
                LOG.info('decision abandoned (material changed): session=%s job=%s',
                         job['session_id'], job['job_id'][:12])
                return False
            tx.outbox.cancel_pending(job['session_id'])
            if reply_valid and not (control_valid and decision.control == 'cancel'):
                basis = {key: valid_reply[key] for key in decision.reply_sentence_keys}
                message_uuid = str(uuid5(NAMESPACE_URL, digest([
                    'adviser-reply-v1', job['session_id'], job['role_version'], basis,
                    decision.reply])))
                recipients = tx.subscriptions.approved_recipients(
                    job['session_id'], session['owner_open_id'])
                tx.outbox.save_reply(
                    message_uuid=message_uuid,
                    session_id=job['session_id'],
                    content=decision.reply,
                    evidence=encode(basis),
                    generation=job['generation'],
                    created_ns=int(now * 1_000_000_000),
                    context_version=session['material_version'],
                    recipients=recipients,
                    now=now)
                LOG.info('reply queued: session=%s uuid=%s recipients=%s reply="%s"',
                         job['session_id'], message_uuid[:12], recipients,
                         decision.reply[:120])
            brief = {
                'generation': job['generation'],
                'snapshot_hash': digest(snapshot),
                'context_version': session['material_version'],
                'evidence': {
                    key: snapshot['evidence'].get(key)
                    for key in set(decision.control_sentence_keys)
                    | set(decision.reply_sentence_keys)},
            }
            tx.jobs.complete(job['job_id'], result=encode(decision.to_dict()),
                             snapshot=encode(brief))
            tx.sessions.complete_decision(job['session_id'], generation=job['generation'])
            if control_valid and decision.control == 'cancel':
                LOG.info('control cancel applied: session=%s job=%s keys=%s',
                         job['session_id'], job['job_id'][:12],
                         list(decision.control_sentence_keys))
        return True
