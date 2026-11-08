"""decision-v1 output validation, mirroring the adviser-side decision schema.

The gateway validates structure only; evidence and freshness remain the
adviser's responsibility (contract section 1).
"""


class InvalidDecision(Exception):
    pass


DECISION_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'properties': {
        'control': {'type': 'string', 'enum': ['none', 'pause', 'resume', 'cancel']},
        'control_sentence_keys': {'type': 'array', 'items': {'type': 'string'}},
        'reply': {'type': ['string', 'null']},
        'reply_sentence_keys': {'type': 'array', 'items': {'type': 'string'}},
    },
    'required': ['control', 'control_sentence_keys', 'reply', 'reply_sentence_keys'],
}


def validate(value) -> dict:
    if not isinstance(value, dict) or set(value) != {
            'control', 'control_sentence_keys', 'reply', 'reply_sentence_keys'}:
        raise InvalidDecision('decision must contain exactly the four contract fields')
    control, reply = value['control'], value['reply']
    if not isinstance(control, str) or control not in ('none', 'pause', 'resume', 'cancel'):
        raise InvalidDecision('control must be one of none/pause/resume/cancel')
    for key in ('control_sentence_keys', 'reply_sentence_keys'):
        items = value[key]
        if (not isinstance(items, list) or len(items) > 100
                or any(not isinstance(item, str) or not item or len(item) > 256
                       for item in items)
                or len(items) != len(set(items))):
            raise InvalidDecision(f'{key} must be a list of unique 1..256 character strings')
    if reply is not None and (not isinstance(reply, str) or not reply.strip()
                              or len(reply.encode('utf-8')) > 4000):
        raise InvalidDecision('reply must be null or non-empty text up to 4000 bytes')
    if (control == 'none') != (not value['control_sentence_keys']):
        raise InvalidDecision('control and control_sentence_keys must agree')
    if (reply is None) != (not value['reply_sentence_keys']):
        raise InvalidDecision('reply and reply_sentence_keys must agree')
    if control in ('pause', 'cancel') and reply is not None:
        raise InvalidDecision('pause and cancel decisions carry no reply')
    return {
        'control': control,
        'control_sentence_keys': list(value['control_sentence_keys']),
        'reply': reply,
        'reply_sentence_keys': list(value['reply_sentence_keys']),
    }
