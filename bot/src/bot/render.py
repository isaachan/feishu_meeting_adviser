"""Feishu outbound rendering: plain text or restricted Markdown (reply-md-v1).

The bot owns presentation. Advisor/Gateway only produce semantic content; this
module turns an OutboundMessage into a Feishu message type and content payload.
Anything doubtful falls back to plain text so a reply is never lost.
"""
from dataclasses import dataclass
import logging
import re


LOG = logging.getLogger(__name__)

MAX_MARKDOWN_BYTES = 12_000
CARD_MARKDOWN_TAG = 'lark_md'

_HTML = re.compile(r'<[^>]*>')
_IMAGE = re.compile(r'!\[([^\]]*)\]\(([^)]*)(?:\s+"[^"]*")?\)')
_LINK = re.compile(r'\[([^\]]+)\]\(([^)]*)(?:\s+"[^"]*")?\)')
_FENCE = re.compile(r'^[ \t]*```[^\n]*$', re.MULTILINE)
_HEADING = re.compile(r'^[ \t]{0,3}#{1,6}[ \t]+(.*)$', re.MULTILINE)
_NESTED_LIST = re.compile(r'^[ \t]+(?=(?:[-*+]|\d+\.)[ \t])', re.MULTILINE)
_TABLE_SEPARATOR = re.compile(r'^[ \t]*\|?[ :|-]+\|?[ \t]*$', re.MULTILINE)
_BLANK_RUN = re.compile(r'\n{3,}')
_MD_SPECIAL = re.compile(r'([\\`*_\[\]<>~|])')


def escape_md(text: str) -> str:
    """Escape inline Markdown specials in user-controlled text."""
    return _MD_SPECIAL.sub(r'\\\1', text)


def meeting_header(title, meeting_no, *, markdown=True) -> str:
    """One-line meeting label for outbound replies: ``标题（会议号 …）``."""
    label = ' '.join((title or '').split())
    if not label:
        return f'会议 {meeting_no}'
    if not markdown:
        return f'{label}（会议号 {meeting_no}）'
    return f'**{escape_md(label)}**（会议号 {meeting_no}）'



class OutboundMessage(str):
    """A message body carrying its rendering format.

    Subclasses ``str`` so existing call sites and tests can keep treating it as
    text while transports read ``format`` to decide whether to render Markdown.
    """

    msg_format: str

    def __new__(cls, text: str, format: str = 'text'):
        message = super().__new__(cls, text)
        message.msg_format = format if format in ('text', 'markdown') else 'text'
        return message


@dataclass(frozen=True)
class Rendered:
    msg_type: str
    content: dict


def render(message: str | OutboundMessage, *, rich_text: bool = True) -> Rendered:
    text = str(message)
    fmt = message.msg_format if isinstance(message, OutboundMessage) else 'text'
    if not rich_text or fmt != 'markdown':
        return _text(text)
    cleaned = sanitize_markdown(text)
    if cleaned is None:
        LOG.warning('markdown render fell back to text (%s bytes)', len(text.encode('utf-8')))
        return _text(text)
    return Rendered('interactive', {
        'config': {'wide_screen_mode': True},
        'elements': [{'tag': 'div',
                      'text': {'tag': CARD_MARKDOWN_TAG, 'content': cleaned}}],
    })


def sanitize_markdown(text: str) -> str | None:
    """Return the reply-md-v1 subset, or None when it must be sent as text."""
    if not isinstance(text, str) or not text.strip():
        return None
    cleaned = text.replace('\r\n', '\n').replace('\r', '\n')
    cleaned = _FENCE.sub('', cleaned)
    cleaned = _HTML.sub('', cleaned)
    cleaned = _IMAGE.sub('', cleaned)
    cleaned = _LINK.sub(_safe_link, cleaned)
    cleaned = _HEADING.sub(lambda match: f"**{match.group(1).strip()}**", cleaned)
    cleaned = _NESTED_LIST.sub('', cleaned)
    cleaned = _TABLE_SEPARATOR.sub('', cleaned)
    cleaned = _BLANK_RUN.sub('\n\n', cleaned)
    cleaned = cleaned.strip()
    if not cleaned or len(cleaned.encode('utf-8')) > MAX_MARKDOWN_BYTES:
        return None
    return cleaned


def _safe_link(match: re.Match) -> str:
    label, url = match.group(1), match.group(2)
    if not re.match(r'https?://', url, re.IGNORECASE):
        return label
    return f'[{label}]({url})'


def _text(text: str) -> Rendered:
    return Rendered('text', {'text': text})
