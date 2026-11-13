"""One explicit test rule; no model, cross-caption inference or background sending."""
from uuid import NAMESPACE_URL, uuid5

from .models import ChangeKind


def deadline_reply(delivery):
    caption = delivery.caption
    if not caption or delivery.kind not in (ChangeKind.ADDED, ChangeKind.UPDATED):
        return None
    if '项目截止日期是明天' not in ''.join(caption.text.split()) or not caption.speaker_name:
        return None
    # Stable across redelivery and revisions of the same sentence in the same meeting.
    message_uuid = str(uuid5(NAMESPACE_URL, repr(('deadline-rule-v1', caption.meeting_id, caption.sentence_key))))
    return message_uuid, f'{caption.speaker_name}，项目截止日期是今天。'
