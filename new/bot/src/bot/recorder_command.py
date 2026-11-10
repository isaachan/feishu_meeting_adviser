"""Recorder command rendering per CONTRACT_RECORDER_STREAM appendix A.

Whitelist-only: values are validated against the appendix charsets before
single-quote injection (charsets exclude quotes/backslashes, so no nested
escaping is needed). The red line is enforced structurally: nothing outside
this module's whitelist can enter the command text, and user credentials have
no source here.
"""
import re

SESSION_ID = re.compile(r'[A-Za-z0-9-]{6,64}')
EVENT_PART = re.compile(r'[A-Za-z0-9_-]{1,128}')
TOKEN = re.compile(r'[A-Za-z0-9_-]{16,128}')
URL = re.compile(r'ws://[A-Za-z0-9._:-]+(:[0-9]+)?/ingest')
SPEAKER = re.compile(r'[^\x00-\x1f\x7f\'"\\]{1,64}')


class CommandRenderError(Exception):
    pass


ALLOWED_KEYS = {'session', 'event', 'url', 'token', 'mic', 'mic_speaker', 'duration'}


def render(args: dict) -> str:
    unknown = set(args) - ALLOWED_KEYS
    if unknown:
        raise CommandRenderError(f'non-whitelist argument keys: {sorted(unknown)}')
    session = args.get('session')
    event = args.get('event')
    url = args.get('url')
    token = args.get('token')
    if not (isinstance(session, str) and SESSION_ID.fullmatch(session)):
        raise CommandRenderError('session id charset rejected')
    if not (isinstance(event, str) and event.count('/') == 1
            and all(EVENT_PART.fullmatch(part) for part in event.split('/'))):
        raise CommandRenderError('event charset rejected')
    if not (isinstance(url, str) and URL.fullmatch(url)):
        raise CommandRenderError('ingest url rejected')
    if not (isinstance(token, str) and TOKEN.fullmatch(token)):
        raise CommandRenderError('stream token charset rejected')
    parts = [f"recorder real --session '{session}'", f"--event '{event}'",
             f"--url '{url}'", f"--token '{token}'"]
    if args.get('mic'):
        parts.append('--mic')
        speaker = args.get('mic_speaker', '本机')
        if not (isinstance(speaker, str) and SPEAKER.fullmatch(speaker)):
            raise CommandRenderError('mic speaker charset rejected')
        if speaker != '本机':
            parts.append(f"--mic-speaker '{speaker}'")
    duration = args.get('duration')
    if duration is not None:
        if not (isinstance(duration, int) and duration > 0):
            raise CommandRenderError('duration must be a positive integer')
        parts.append(f'--duration {duration}')
    return ' \\\n  '.join(parts)
