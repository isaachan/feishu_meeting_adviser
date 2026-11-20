"""Owner-token observation of ordinary Feishu meetings (SM-08).

The user-token fallback follows studies/meeting-events/poll_user.py. Absence
from user_active_meeting only means "not currently resolvable"; it never
proves a departure. Only an explicit participant status=4 in a bound meeting
can pause a session. All other unavailable observations are unknown.
"""
import json
import logging
import time
from urllib import error, parse, request

from .auth_client import AuthUnavailable, AuthorizationRequired

LOG = logging.getLogger(__name__)
BASE_URL = 'https://open.feishu.cn/open-apis'
VC_SCOPE = 'vc:meeting.meetingevent:read'
MAX_BODY = 262_144
TOKEN_INVALID = {20005, 99991661, 99991663, 99991668, 99991677}
LIST_BY_NO_DENIED = {99991672, 99991679}


class ObservationUnavailable(Exception):
    def __init__(self, code=None):
        self.code = code
        super().__init__('meeting observation unavailable')


def _timestamp(value):
    if isinstance(value, dict):
        value = value.get('timestamp')
    if isinstance(value, str) and value.isdecimal():
        value = int(value)
    return value if type(value) is int and 0 < value < 10**11 else None


class FeishuMeetingObserver:
    def __init__(self, store, auth, *, base_url=BASE_URL, timeout_s=10,
                 max_age_s=60, clock=time.time, opener=None):
        self.store, self.auth = store, auth
        self.base_url = base_url.rstrip('/')
        self.timeout_s = timeout_s
        self.max_age_s = max_age_s
        self.clock = clock
        self._open = opener or request.build_opener(request.ProxyHandler({})).open
        self._user_ids = {}
        self._lookup_denied = False
        self._notified_at = {}

    def _get(self, path, token, query=None):
        url = self.base_url + path
        if query:
            url += '?' + parse.urlencode(query)
        req = request.Request(url, headers={
            'Authorization': 'Bearer ' + token, 'Accept': 'application/json'})
        try:
            with self._open(req, timeout=self.timeout_s) as response:
                raw = response.read(MAX_BODY + 1)
        except error.HTTPError as exc:
            try:
                raw = exc.read(MAX_BODY + 1)
                result = json.loads(raw) if len(raw) <= MAX_BODY else {}
            except (ValueError, OSError):
                result = {}
            raise ObservationUnavailable(result.get('code') if isinstance(result, dict)
                                         else None) from None
        except (error.URLError, TimeoutError, OSError):
            raise ObservationUnavailable() from None
        try:
            result = json.loads(raw) if len(raw) <= MAX_BODY else {}
        except (ValueError, UnicodeDecodeError):
            raise ObservationUnavailable() from None
        if not isinstance(result, dict) or result.get('code') != 0:
            raise ObservationUnavailable(result.get('code') if isinstance(result, dict) else None)
        data = result.get('data')
        if not isinstance(data, dict):
            raise ObservationUnavailable()
        return data

    def _ids(self, owner, token):
        if owner not in self._user_ids:
            data = self._get('/authen/v1/user_info', token)
            ids = {data[key] for key in ('open_id', 'user_id', 'union_id')
                   if isinstance(data.get(key), str) and data[key]}
            if owner not in ids:
                raise ObservationUnavailable()
            self._user_ids[owner] = ids
        return self._user_ids[owner]

    def _active_ids(self, meeting_no, token):
        data = self._get('/vc/v1/bots/user_active_meeting', token)
        meetings = data.get('meetings')
        if not isinstance(meetings, list):
            raise ObservationUnavailable()
        return [str(item['meeting_id']) for item in meetings
                if isinstance(item, dict) and str(item.get('meeting_no')) == meeting_no
                and item.get('meeting_id')]

    def _resolve(self, meeting_no, token):
        if not self._lookup_denied:
            now = int(self.clock())
            try:
                data = self._get('/vc/v1/meetings/list_by_no', token, {
                    'meeting_no': meeting_no, 'start_time': now - 3600,
                    'end_time': now, 'page_size': 50})
                if data.get('has_more'):
                    raise ObservationUnavailable()  # ambiguous/incomplete page
                briefs = data.get('meeting_briefs')
                if not isinstance(briefs, list):
                    raise ObservationUnavailable()
                return list(dict.fromkeys(str(item['id']) for item in briefs
                                          if isinstance(item, dict) and item.get('id')))
            except ObservationUnavailable as exc:
                if exc.code not in LIST_BY_NO_DENIED:
                    raise
                self._lookup_denied = True
        return self._active_ids(meeting_no, token)

    def _details(self, meeting_id, token):
        data = self._get('/vc/v1/meetings/' + parse.quote(meeting_id, safe=''),
                         token, {'with_participants': 'true'})
        meeting = data.get('meeting')
        if not isinstance(meeting, dict):
            raise ObservationUnavailable()
        return meeting

    def _observe(self, session, token, now):
        owner = session['owner_open_id']
        owner_ids = self._ids(owner, token)
        meeting_id = session['vc_meeting_id']
        if not meeting_id:
            candidates = self._resolve(session['meeting_no'], token)
            if len(candidates) != 1:
                return  # no VC ID before owner enters, or multiple candidates
            meeting_id = candidates[0]
        meeting = self._details(meeting_id, token)
        if str(meeting.get('meeting_no')) != session['meeting_no']:
            return
        if session['vc_meeting_id'] is None:
            # Bind only if the owner is positively seen in this occurrence.
            # VC scheduled start, when provided, must match the calendar slot.
            start = _timestamp(meeting.get('start_time'))
            try:
                scheduled = int(session['meeting_instance_key'].rsplit('|', 1)[1])
            except (ValueError, IndexError):
                scheduled = 0  # migrated legacy key; no stable occurrence start
            if start and scheduled and abs(start - scheduled) > 3600:
                return
            if self._presence(meeting, owner_ids) != 'present':
                return
            if not self.store.bind_vc_meeting(session['session_id'], meeting_id):
                return
        status = meeting.get('status')
        presence = self._presence(meeting, owner_ids)
        self.store.apply_observation(
            session['session_id'], owner_presence=presence,
            meeting_status='ended' if status == 3 else 'in_progress' if status == 2 else 'unknown',
            observed_at=now, now=self.clock(), max_age_s=self.max_age_s)

    @staticmethod
    def _presence(meeting, owner_ids):
        people = meeting.get('participants')
        if not isinstance(people, list):
            return 'unknown'
        matched = [person for person in people if isinstance(person, dict)
                   and str(person.get('id')) in owner_ids]
        if len(matched) != 1:
            return 'unknown'  # absent ≠ confirmed left
        return {2: 'present', 4: 'left'}.get(matched[0].get('status'), 'unknown')

    def observe(self, session):
        owner = session['owner_open_id']
        try:
            credential = self.auth.get_token(owner)
            if VC_SCOPE not in credential.get('scopes', []):
                self._notify_reauthorize(session)
                return
            token = credential['access_token']
            try:
                self._observe(session, token, self.clock())
            except ObservationUnavailable as exc:
                if exc.code not in TOKEN_INVALID:
                    raise
                credential = self.auth.get_token(owner, rejected_token=token)
                self._observe(session, credential['access_token'], self.clock())
        except AuthorizationRequired:
            self._notify_reauthorize(session)
        except (AuthUnavailable, ObservationUnavailable):
            LOG.info('meeting observation unknown: session=%s', session['session_id'])

    def _notify_reauthorize(self, session):
        now = self.clock()
        if now - self._notified_at.get(session['session_id'], 0) < 3600:
            return
        try:
            url = self.auth.authorize_url(session['owner_open_id'], 'meeting:observe')
            self.store.insert_owner_notice(
                session['session_id'], session['owner_open_id'],
                '会参谋暂时无法确认你是否在会。请打开链接重新授权飞书：\n' + url,
                now=now, dedupe_s=3600, provider='meeting-observer')
            self._notified_at[session['session_id']] = now
        except (AuthUnavailable, error.HTTPError, error.URLError, OSError, KeyError):
            LOG.info('meeting observation authorization unavailable: session=%s',
                     session['session_id'])
