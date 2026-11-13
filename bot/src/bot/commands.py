"""Command parsing for the bot control plane (rule-based, no LLM).

Meeting-reference rules are ported verbatim from the legacy advisor
``parse_meeting_reference`` (nine-digit number or https://vc.feishu.cn/j/ link
with optional single ``pwd`` query parameter).
"""
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlsplit

MEETING_LINK_HOST = 'vc.feishu.cn'
_MEETING_NO = re.compile(r'[0-9]{9}')
_REQUEST_NO = re.compile(r'#r?([0-9]{1,9})')
_MENTION = re.compile(r'@[^@\s]+\s?')


class CommandError(Exception):
    def __init__(self, hint):
        self.hint = hint
        super().__init__(hint)


def validate_meeting_no(value) -> str:
    if not isinstance(value, str) or not _MEETING_NO.fullmatch(value):
        raise CommandError('会议号必须是 9 位数字')
    return value


def parse_meeting_reference(value) -> tuple[str, str]:
    """Returns (meeting_no, password); ported from the legacy advisor rules."""
    if not isinstance(value, str) or not value.strip():
        raise CommandError('需要 9 位会议号或会议链接')
    text = value.strip()
    if '://' not in text:
        return validate_meeting_no(text), ''
    try:
        url = urlsplit(text)
    except ValueError:
        raise CommandError('会议链接无效') from None
    if (url.scheme != 'https' or url.hostname != MEETING_LINK_HOST
            or url.username or url.password or url.fragment):
        raise CommandError(f'会议链接必须是 https://{MEETING_LINK_HOST}/j/ 形式')
    match = re.fullmatch(r'/j/([0-9]{9})', url.path.rstrip('/'))
    if not match:
        raise CommandError('会议链接必须指向 9 位会议号')
    passwords = [item for key, item in parse_qsl(url.query, keep_blank_values=True)
                 if key == 'pwd' and item]
    if len(set(passwords)) > 1:
        raise CommandError('会议链接携带了多个不同的密码')
    password = passwords[-1] if passwords else ''
    if len(password) > 64 or not password.isprintable():
        raise CommandError('会议链接中的密码无效')
    return match.group(1), password


@dataclass(frozen=True, slots=True)
class Command:
    kind: str            # join|end|approve|deny|revoke|status|subscribe|help
    meeting_no: str = ''
    password: str = ''
    request_no: int = 0
    subscriber_name: str = ''


def strip_mentions(text: str) -> str:
    return _MENTION.sub('', text).strip()


def parse_command(raw_text: str) -> Command | None:
    """None for unmatched text (help reply); raises CommandError for bad refs."""
    text = strip_mentions(raw_text or '')
    if not text:
        return None
    parts = text.split()
    head = parts[0]

    if head == '入会' and len(parts) >= 2:
        no, password = parse_meeting_reference(' '.join(parts[1:]))
        return Command('join', no, password)
    if head == '结束会议':
        if len(parts) == 1:
            return Command('end')
        no, _ = parse_meeting_reference(parts[1])
        return Command('end', no)
    if head in ('同意', '拒绝'):
        if len(parts) >= 2:
            match = _REQUEST_NO.fullmatch(parts[1].strip())
            if not match:
                raise CommandError(f'{head}需要形如"同意 #rN"的审批编号')
            number = int(match.group(1))
        else:
            number = 0  # act on the newest pending request
        return Command('approve' if head == '同意' else 'deny', request_no=number)
    if head in ('取消订阅', '退订'):
        if len(parts) >= 2:
            no, _ = parse_meeting_reference(parts[1])
            return Command('unsubscribe', no)
        return Command('unsubscribe')
    if head == '撤销' and len(parts) >= 2 and parts[1].startswith('#'):
        match = _REQUEST_NO.fullmatch(parts[1])
        if not match:
            raise CommandError('撤销需要形如"撤销 #rN"的审批编号')
        return Command('revoke', request_no=int(match.group(1)))
    if head == '撤销' and len(parts) == 1:
        return Command('revoke')  # unique approved subscription, else prompt
    if text == '状态':
        return Command('status')
    if text in ('会议列表', '会议'):
        return Command('meetings')
    # subscription intent: unmatched text that references a meeting
    for token in parts:
        if _MEETING_NO.fullmatch(token) or '://' in token:
            no, password = parse_meeting_reference(token)
            return Command('subscribe', no, password)
    return None
