"""Session discovery payload normalization (framework-free).

/gui/sessions responses are validated and normalized into a display model:
user {open_id, name, email} + sessions [{session_id, meeting_no, title, phase,
started_at, recorder_command_args|null}].
"""
from .api import NetworkError

KNOWN_PHASES = ('recording', 'draining')
ARGS_KEYS = {'session', 'event', 'url', 'token', 'mic'}


def normalize_sessions(payload):
    if not isinstance(payload, dict):
        raise NetworkError('sessions payload must be an object')
    user = payload.get('user') or {}
    if not isinstance(user, dict) or not user.get('open_id'):
        raise NetworkError('sessions payload missing user identity')
    raw_sessions = payload.get('sessions')
    if not isinstance(raw_sessions, list):
        raise NetworkError('sessions payload missing sessions list')
    sessions = []
    for item in raw_sessions:
        session = _normalize_item(item)
        if session is not None:
            sessions.append(session)
    return {'user': {'open_id': user.get('open_id'), 'name': user.get('name'),
                     'email': user.get('email')},
            'sessions': sessions}


def _normalize_item(item):
    if not isinstance(item, dict):
        return None
    session_id = item.get('session_id')
    phase = item.get('phase')
    if not isinstance(session_id, str) or not session_id or phase not in KNOWN_PHASES:
        return None
    args = item.get('recorder_command_args')
    if args is not None:
        if not isinstance(args, dict) or not ARGS_KEYS <= set(args):
            return None
    return {'session_id': session_id,
            'meeting_no': item.get('meeting_no') or '',
            'title': item.get('title') or session_id,
            'phase': phase,
            'started_at': item.get('started_at'),
            'recorder_command_args': args}
