"""decision-v1 validation, mirroring the adviser Decision.parse rules."""

DECISION_FIELDS = ('control', 'control_sentence_keys', 'reply', 'reply_sentence_keys')


def validate_decision(value) -> tuple[dict | None, str | None]:
    """Return (decision, None) when valid, else (None, reason)."""
    if not isinstance(value, dict) or set(value) != set(DECISION_FIELDS):
        return None, '必须恰好包含 ' + ', '.join(DECISION_FIELDS)
    control, reply = value['control'], value['reply']
    if not isinstance(control, str) or control not in ('none', 'cancel'):
        return None, 'control 必须是 none 或 cancel'
    for key in ('control_sentence_keys', 'reply_sentence_keys'):
        items = value[key]
        if not isinstance(items, list) or len(items) > 100 or any(
                not isinstance(x, str) or not x or len(x) > 256 for x in items):
            return None, f'{key} 必须是最多 100 个非空短字符串'
        if len(items) != len(set(items)):
            return None, f'{key} 含重复项'
    if reply is not None and (not isinstance(reply, str) or not reply.strip()
                              or len(reply.encode('utf-8')) > 4000):
        return None, 'reply 必须为 null 或 1..4000 字节文本'
    if (control == 'none') != (not value['control_sentence_keys']):
        return None, 'control_sentence_keys 必须与 control 是否为 none 一致'
    if (reply is None) != (not value['reply_sentence_keys']):
        return None, 'reply_sentence_keys 必须与 reply 是否为 null 一致'
    if control == 'cancel' and reply is not None:
        return None, 'cancel 判断不携带 reply'
    return value, None
