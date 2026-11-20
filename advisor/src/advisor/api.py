"""Advisor HTTP API on 127.0.0.1:8380 per CONTRACT_ADVISOR_API v1.

Ports (auth client, calendar validator) are injected; the calendar validator
interface returns the binding + title + description on success and raises
CalendarError(code) for the four S1 rejection paths.
"""
import hashlib
import json
import logging
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import parse

from .auth_client import AuthorizationRequired, AuthUnavailable
from .errors import StateError, StorageError
from .nsdp import NsdpUnavailable
from .persistence import meeting_instance_key
from .store import SessionStore

LOG = logging.getLogger(__name__)

INGEST_PORT = 8400


class CalendarError(Exception):
    def __init__(self, code):
        self.code = code  # meeting_not_found | not_organizer | outside_window
        super().__init__(code)


class CalendarValidator:
    def validate(self, owner_open_id, meeting_ref) -> dict:
        """Returns dict(event_calendar_id, event_id, title, description)."""
        raise NotImplementedError

    def chat_member_hit(self, owner_open_id, binding, caller_ids) -> bool:
        """v2.6: caller invited via a chat attendee (lazy expansion)."""
        raise NotImplementedError


class StubCalendarValidator(CalendarValidator):
    """Deterministic occurrence model for tests and the integration bed.

    ``occurrences`` maps meeting_no -> list of occurrence dicts
    ({start_ts, organizer_open_id?, attendees?, chat_attendee_ids?,
    chat_members?, title?, description?, event_id?}). ``chat_members`` maps
    chat attendee_id -> member id collection for the v2.6 group-invite
    admission path. Default (no registry): one implicit occurrence per
    meeting_no that anyone can organize (stub-first stages; preserves the
    pre-v2 stub behavior).
    """

    DEFAULT_START_TS = 1762800000

    def __init__(self, description='', occurrences=None):
        self.description = description
        self.occurrences = occurrences

    def _occurrences_for(self, meeting_no):
        if self.occurrences is not None:
            return self.occurrences.get(meeting_no, [])
        return [{'start_ts': self.DEFAULT_START_TS, 'organizer_open_id': None}]

    def validate(self, owner_open_id, meeting_ref):
        meeting_no = (meeting_ref or {}).get('no')
        occurrences = self._occurrences_for(meeting_no)
        if not occurrences:
            raise CalendarError('meeting_not_found')
        if len(occurrences) > 1:
            raise CalendarError('meeting_ambiguous')
        occurrence = occurrences[0]
        organizer = occurrence.get('organizer_open_id')
        organizer_ok = organizer is None or organizer == owner_open_id
        attendees = set(occurrence.get('attendees') or ())
        return {'event_calendar_id': 'cal_stub',
                'event_id': occurrence.get('event_id') or f'evt_{meeting_no}',
                'title': occurrence.get('title') or f'会议 {meeting_no}',
                'description': occurrence.get('description', self.description),
                'organizer': organizer if organizer is not None else owner_open_id,
                'organizer_ok': organizer_ok,
                'attendees': attendees,
                'chat_attendee_ids': list(occurrence.get('chat_attendee_ids') or ()),
                'chat_members': {chat_id: set(members) for chat_id, members
                                 in (occurrence.get('chat_members') or {}).items()},
                'caller_ids': {owner_open_id},
                'start': occurrence.get('start_ts', self.DEFAULT_START_TS),
                'end': occurrence.get('start_ts', self.DEFAULT_START_TS) + 3600}

    def chat_member_hit(self, owner_open_id, binding, caller_ids):
        members_map = binding.get('chat_members') or {}
        for attendee_id in binding.get('chat_attendee_ids') or ():
            if set(members_map.get(attendee_id) or ()) & set(caller_ids):
                return True
        return False

    def list_meetings(self, owner_open_id, *, days=7, limit=10):
        now = int(time.time())
        return {'now': now, 'meetings': [{
            'meeting_no': '123456789', 'title': '桩会议', 'start_ts': now + 3600,
            'end_ts': now + 7200, 'is_organizer': True, 'in_progress': False}]}


class ApiError(Exception):
    def __init__(self, status, code, message, **extra):
        self.status, self.code, self.message = status, code, message
        self.extra = extra
        super().__init__(code)


def _state_error(exc: StateError):
    mapping = {'not_found': (404, 'not_found'), 'invalid_state': (409, 'invalid_state'),
               'not_owner': (409, 'not_owner'), 'session_not_recording': (409, 'session_ended'),
               'session_claimed': (409, 'session_claimed'),
               'instance_ended': (409, 'instance_ended'),
               'claim_conflict': (409, 'claim_conflict')}
    status, code = mapping.get(exc.args[0] if exc.args else '', (409, 'invalid_state'))
    return ApiError(status, code, 'state machine rejected the operation')


class AdvisorApi:
    """Domain glue between HTTP and store/supervisor; testable without HTTP."""

    def __init__(self, store: SessionStore, supervisor, *,
                 auth_client, calendar_validator, ingest_url, host='127.0.0.1',
                 clock=time.time, nsdp=None, nsdp_join_budget_s=12.0,
                 legacy_nsdp=False, email_domain='nio.com', approval_ttl_s=1800,
                 drain_deadline_s=600, idle_timeout_s=900):
        self.store = store
        self.supervisor = supervisor
        self.auth = auth_client
        self.calendar = calendar_validator
        self.ingest_url = ingest_url.rstrip('/')
        self.host = host
        self.clock = clock
        self.nsdp = nsdp
        self.legacy_nsdp = legacy_nsdp
        self.nsdp_join_budget_s = nsdp_join_budget_s
        self.email_domain = email_domain
        self.approval_ttl_s = approval_ttl_s
        self.drain_deadline_s = drain_deadline_s
        self.idle_timeout_s = idle_timeout_s
        self._gui_logins: dict = {}
        self._gui_lock = threading.Lock()

    def _gate_nsdp(self, owner):
        """Organizer-surface commands require an NSDP token (hard gate).

        小B (subscriber surface) never passes through here.
        """
        if self.nsdp is None or not self.legacy_nsdp:
            return
        try:
            self.nsdp.require_token(owner)
        except AuthorizationRequired as exc:
            raise ApiError(409, 'authorization_required', 'nsdp token missing/expired',
                           provider='nsdp', submit_url=exc.url) from None
        except AuthUnavailable:
            raise ApiError(409, 'auth_unavailable', 'auth node unreachable') from None

    def with_idempotency(self, key, action):
        if not key or not isinstance(key, str) or len(key) > 128:
            raise ApiError(400, 'bad_message', 'missing or invalid idempotency key')
        row = self.store.db.execute(
            'SELECT response FROM idempotency WHERE key=?', (key,)).fetchone()
        if row is not None:
            return json.loads(row['response']), True
        response = action()
        with self.store.unit_of_work() as tx:
            tx.connection.execute(
                'INSERT OR IGNORE INTO idempotency(key,response,created_ns) VALUES (?,?,?)',
                (key, json.dumps(response, ensure_ascii=False), int(self.clock() * 1e9)))
        return response, False

    # -- POST /sessions ------------------------------------------------------

    def _gate_join_credentials(self, owner, meeting_no):
        missing = []
        for provider, getter in (('feishu', self.auth.get_token),
                                 ('nsdp', self.auth.get_nsdp_token),
                                 ('jira', self.auth.get_jira_token)):
            try:
                getter(owner)
            except AuthorizationRequired as exc:
                if provider == 'feishu':
                    try:
                        url = self.auth.authorize_url(owner, f'join:{meeting_no}')
                    except Exception:
                        raise ApiError(409, 'auth_unavailable', 'auth node unreachable') from None
                    missing.append({'provider': provider, 'authorize_url': url})
                else:
                    missing.append({'provider': provider, 'submit_url': exc.url})
            except AuthUnavailable:
                raise ApiError(409, 'auth_unavailable', 'auth node unreachable') from None
        if missing:
            first = missing[0]
            raise ApiError(409, 'authorization_required', 'join credentials missing',
                           missing_credentials=missing, **first)

    def create_session(self, body):
        owner = body.get('owner_open_id')
        meeting_ref = body.get('meeting_ref') or {}
        meeting_no = meeting_ref.get('no')
        if not isinstance(owner, str) or not owner or not isinstance(meeting_no, str) \
                or not meeting_no.isdigit() or not 6 <= len(meeting_no) <= 12:
            raise ApiError(400, 'bad_message', 'owner_open_id and meeting_ref.no required')

        if self.legacy_nsdp:
            self._gate_nsdp(owner)
        self._gate_join_credentials(owner, meeting_no)
        # Cheap retry shortcut (same owner, same meeting_no, non-ended): the
        # single-claim rule means this must be the caller's session for that
        # meeting; avoids the calendar round-trip on command retries.
        active = self.store.db.execute(
            "SELECT * FROM sessions WHERE owner_open_id=? AND meeting_no=? "
            "AND phase IN ('running','paused','draining')", (owner, meeting_no)).fetchone()
        if active is not None:
            LOG.info('session reuse: session=%s meeting=%s owner=%s',
                     active['session_id'], meeting_no, owner)
            return self._session_response(dict(active), existing=True)

        try:
            binding = self.calendar.validate(owner, meeting_ref)
        except CalendarError as exc:
            raise ApiError(409, exc.code, 'calendar validation failed') from None

        # Admission (STATE_MACHINES v2 §0): A organizer path first, then the
        # C path (enabled grant + invite-list hit on the same occurrence,
        # directly or via an invited chat group — v2.6).
        if not binding.get('organizer_ok', True):
            if owner not in self.store.enabled_supervisor_open_ids():
                raise ApiError(409, 'not_supervisor',
                               'caller is neither the organizer nor a supervisor')
            attendees = binding.get('attendees') or set()
            caller_ids = binding.get('caller_ids') or {owner}
            if not (attendees & caller_ids) \
                    and not self._chat_invite_hit(owner, binding, caller_ids):
                raise ApiError(409, 'not_invited',
                               'supervisor is not on this occurrence invite list')

        nsdp_text = ''
        if self.nsdp is not None and self.legacy_nsdp:
            try:
                # Short join budget: degrade fast — the supervisor backfills.
                nsdp_text = self.nsdp.fetch(owner, binding.get('description') or '',
                                             budget_s=self.nsdp_join_budget_s)
            except AuthorizationRequired as exc:
                raise ApiError(409, 'authorization_required', 'nsdp token missing/expired',
                               provider='nsdp', submit_url=exc.url) from None
            except AuthUnavailable:
                raise ApiError(409, 'auth_unavailable', 'auth node unreachable') from None
            except NsdpUnavailable as exc:
                # T7 degrade (2026-11-13): join proceeds without NSDP material;
                # the supervisor's first maintenance pass backfills it, where a
                # rejected token surfaces as nsdp_hold (implicit validation).
                LOG.warning('nsdp fetch degraded, joining without material: %s', exc)
                nsdp_text = ''

        try:
            session, existing = self.store.create_session(
                owner_open_id=owner, meeting_no=meeting_no,
                title=binding['title'], event_calendar_id=binding['event_calendar_id'],
                event_id=binding['event_id'], description=binding.get('description') or '',
                now=self.clock(), nsdp_text=nsdp_text,
                idle_timeout_s=self._effective_idle_timeout_s(),
                instance_key=meeting_instance_key(
                    meeting_no, binding['event_calendar_id'], binding['event_id'],
                    binding.get('start') or 0),
                organizer_open_id=binding.get('organizer') or owner)
        except StateError as exc:
            failure = _state_error(exc)
            if failure.code == 'claim_conflict':
                failure.extra['conflicting_title'] = self.store.claimed_session_title(owner)
            raise failure from None
        assert session is not None
        if not existing:
            self.supervisor.start_session(session['session_id'])
        LOG.info('session created: session=%s meeting=%s owner=%s instance=%s existing=%s',
                 session['session_id'], meeting_no, owner,
                 session['meeting_instance_key'], existing)
        return self._session_response(session, existing=existing)

    def _chat_invite_hit(self, owner, binding, caller_ids):
        """v2.6: invite proof via an invited chat group (lazy expansion).

        Only reached when the organizer and direct-invite checks missed;
        validators without chat attendees (empty chat_attendee_ids, the
        pre-v2.6 shape) never trigger the expansion calls.
        """
        if not binding.get('chat_attendee_ids'):
            return False
        try:
            return bool(self.calendar.chat_member_hit(owner, binding, caller_ids))
        except CalendarError as exc:
            raise ApiError(409, exc.code, 'calendar validation failed') from None

    def _session_response(self, session, existing=False):
        token_row = self.store.session_token(session['session_id'])
        assert token_row is not None
        return {
            'session_id': session['session_id'], 'meeting_no': session['meeting_no'],
            'title': session['title'], 'phase': session['phase'],
            'meeting_instance_key': session['meeting_instance_key'],
            'started_at': session['started_at'], 'existing': existing,
            'recorder_command_args': {
                'session': session['session_id'],
                'event': f"{session['event_calendar_id']}/{session['event_id']}",
                'url': self.ingest_url, 'token': token_row['stream_token'],
            }}

    # -- meetings (plan-C T1) ---------------------------------------------------

    def list_meetings(self, owner):
        if not isinstance(owner, str) or not owner:
            raise ApiError(400, 'bad_message', 'owner_open_id required')
        self._gate_nsdp(owner)
        try:
            self.auth.get_token(owner)  # credentials gate (same as join)
        except AuthorizationRequired:
            url = self.auth.authorize_url(owner, 'list:meetings')
            raise ApiError(409, 'authorization_required', 'user token missing/expired',
                           authorize_url=url) from None
        except AuthUnavailable:
            raise ApiError(409, 'auth_unavailable', 'auth node unreachable') from None
        try:
            return self.calendar.list_meetings(owner)
        except CalendarError as exc:
            raise ApiError(409, exc.code, 'calendar read failed') from None

    # -- sessions ------------------------------------------------------------

    def list_sessions(self, owner):
        if not isinstance(owner, str) or not owner:
            raise ApiError(400, 'bad_message', 'owner_open_id required')
        self._gate_nsdp(owner)
        sessions = self.store.list_sessions(owner)
        return {'sessions': [{'session_id': s['session_id'], 'meeting_no': s['meeting_no'],
                              'title': s['title'], 'phase': s['phase'],
                              'started_at': s['started_at']} for s in sessions]}

    def session_status(self, session_id):
        try:
            return self.store.status(session_id, now=self.clock())
        except StorageError:
            raise ApiError(404, 'not_found', 'unknown session') from None

    def end_session(self, session_id, body):
        owner = body.get('owner_open_id')
        if isinstance(owner, str) and owner:
            self._gate_nsdp(owner)
        try:
            session = self.store.require_session(session_id)
        except StorageError:
            raise ApiError(404, 'not_found', 'unknown session') from None
        if owner != session['owner_open_id']:
            raise ApiError(409, 'not_owner', 'only the organizer may end this session')
        if session['phase'] in ('ended', 'draining'):
            return {'session_id': session_id, 'phase': session['phase']}
        updated = self.store.set_phase(session_id, 'draining', now=self.clock(),
                                       drain_deadline_at=self.clock() + self.drain_deadline_s)
        assert updated is not None
        LOG.info('session ending: session=%s -> draining (owner=%s)', session_id, owner)
        return {'session_id': session_id, 'phase': updated['phase']}

    # -- supervisor grants (CONTRACT_ADVISOR_API §9.3/§9.4) --------------------

    def _resolve_grant_identity(self, login_name):
        """login_name -> unique open_id via the identity directory.

        Accepts an ``ou_`` open_id directly, a full email, or a bare login
        that is matched against the directory's Feishu ``user_id`` (login
        name, CONTRACT_AUTH v1.7) and then completed with the configured
        default email domain. Returns None when the directory cannot resolve
        it uniquely (the grant row is then stored disabled with open_id NULL,
        per §9.4).
        """
        if not isinstance(login_name, str) or not login_name.strip():
            return None
        candidate = login_name.strip()
        if candidate.startswith('ou_'):
            return candidate
        rows = self.store.db.execute(
            'SELECT open_id FROM users WHERE user_id IS NOT NULL '
            'AND lower(user_id)=lower(?)', (candidate,)).fetchall()
        if len(rows) == 1:
            return rows[0]['open_id']
        if len(rows) > 1:  # ambiguous login names never resolve
            return None
        email = candidate if '@' in candidate else f'{candidate}@{self.email_domain}'
        rows = self.store.db.execute(
            'SELECT open_id FROM users WHERE lower(email)=lower(?)', (email,)).fetchall()
        if len(rows) == 1:
            return rows[0]['open_id']
        return None

    def _refresh_directory_logins(self):
        """Best-effort pull of auth profiles for pre-v1.7 directory rows.

        Runs when a supervisor login cannot be resolved: the auth profile may
        already carry the Feishu user_id (CONTRACT_AUTH v1.7 lazy backfill).
        Degrades silently — resolution then simply fails as before.
        """
        for open_id in self.store.users_missing_user_id():
            try:
                profile = self.auth.user_profile(open_id)
            except AuthUnavailable:
                return
            if profile is None:
                self.store.upsert_user(open_id, now=self.clock())
                continue
            self.store.upsert_user(open_id, name=profile.get('name'),
                                   en_name=profile.get('en_name'),
                                   email=profile.get('email'),
                                   user_id=profile.get('user_id'), now=self.clock())

    def admin_supervisors(self):
        rows = self.store.supervisor_grants()
        return {'supervisors': [
            {'login_name': row['login_name'], 'open_id': row['open_id'],
             'enabled': bool(row['enabled']),
             'updated_at': row['updated_ns'] / 1e9,
             'updated_by': row['updated_by']} for row in rows]}

    def admin_put_supervisor(self, body):
        login_name = body.get('login_name')
        enabled = body.get('enabled')
        if not isinstance(login_name, str) or not login_name.strip() \
                or not isinstance(enabled, bool):
            raise ApiError(400, 'bad_message', 'login_name and enabled required')
        open_id = self._resolve_grant_identity(login_name)
        if enabled and open_id is None and not login_name.strip().startswith('ou_'):
            # the directory may predate CONTRACT_AUTH v1.7 login capture
            self._refresh_directory_logins()
            open_id = self._resolve_grant_identity(login_name)
        if enabled and open_id is None:
            # keep an audit trail but never enable an unresolved identity
            self.store.put_supervisor_grant(login_name.strip(), open_id=None,
                                            enabled=False, now=self.clock())
            raise ApiError(409, 'identity_unresolved',
                           'login does not resolve to a unique known open_id; '
                           'the user must authorize/login once first')
        row = self.store.put_supervisor_grant(login_name.strip(), open_id=open_id,
                                              enabled=enabled, now=self.clock())
        LOG.info('supervisor grant: login=%s open_id=%s enabled=%s',
                 row['login_name'], row['open_id'], row['enabled'])
        return {'login_name': row['login_name'], 'open_id': row['open_id'],
                'enabled': bool(row['enabled']),
                'updated_at': row['updated_ns'] / 1e9, 'updated_by': row['updated_by']}

    # -- admin config (CONTRACT_ADVISOR_API §9.5) ------------------------------

    IDLE_TIMEOUT_MIN_S = 60
    IDLE_TIMEOUT_MAX_S = 86400

    def _effective_idle_timeout_s(self) -> int:
        """Runtime config wins over the process default (§9.5); applied to
        new sessions only — existing rows keep their persisted value."""
        stored = self.store.app_config_value('idle_timeout_s')
        return self.idle_timeout_s if stored is None else int(stored)

    def _config_response(self, row):
        return {'idle_timeout_s': self._effective_idle_timeout_s(),
                'default_idle_timeout_s': self.idle_timeout_s,
                'updated_at': row['updated_ns'] / 1e9 if row else None,
                'updated_by': row['updated_by'] if row else None}

    def admin_get_config(self):
        return self._config_response(self.store.app_config_row('idle_timeout_s'))

    def admin_put_config(self, body):
        value = body.get('idle_timeout_s')
        if not isinstance(value, int) or isinstance(value, bool) \
                or not self.IDLE_TIMEOUT_MIN_S <= value <= self.IDLE_TIMEOUT_MAX_S:
            raise ApiError(400, 'bad_message',
                           f'idle_timeout_s must be an integer in '
                           f'[{self.IDLE_TIMEOUT_MIN_S}, {self.IDLE_TIMEOUT_MAX_S}]')
        row = self.store.put_app_config('idle_timeout_s', value, now=self.clock())
        LOG.info('admin config: idle_timeout_s=%s (applies to new sessions only)', value)
        return self._config_response(row)

    # -- admin face (CONTRACT_ADVISOR_API §9) --------------------------------

    def _identity_directory(self, open_ids):
        """open_id -> {name, email, ...} for display; backfills from auth
        profiles (CONTRACT_AUTH §2.4), degrading to open_id-only when auth is
        unreachable or the user never authorized (tombstone, 5min re-check)."""
        directory = self.store.users_map(open_ids)
        now = self.clock()
        now_ns = int(now * 1_000_000_000)
        for open_id in sorted(open_ids):
            row = directory.get(open_id)
            stale = (row is None or row['checked_ns'] == 0  # migration seed: never checked
                     or ((row['name'] is None or row.get('user_id') is None)
                         and now_ns - row['checked_ns'] > 300 * 1_000_000_000))
            if not stale:
                continue
            try:
                profile = self.auth.user_profile(open_id)
            except AuthUnavailable:
                continue
            if profile is None and row is not None:
                self.store.upsert_user(open_id, now=now)  # tombstone refresh
                directory[open_id] = row | {'checked_ns': now_ns}
                continue
            self.store.upsert_user(open_id, name=profile.get('name') if profile else None,
                                   en_name=profile.get('en_name') if profile else None,
                                   email=profile.get('email') if profile else None,
                                   user_id=profile.get('user_id') if profile else None,
                                   now=now)
            directory[open_id] = {'open_id': open_id,
                                  'name': profile.get('name') if profile else None,
                                  'en_name': profile.get('en_name') if profile else None,
                                  'email': profile.get('email') if profile else None,
                                  'user_id': profile.get('user_id') if profile else None,
                                  'checked_ns': now_ns}
        return directory

    def admin_sessions(self):
        """All owners, all phases incl. ended; ops listing, no NSDP gate."""
        sessions = self.store.list_sessions_all()
        subscriptions = self.store.all_subscriptions()
        ids = {s['owner_open_id'] for s in sessions}
        ids |= {s.get('organizer_open_id') for s in sessions if s.get('organizer_open_id')}
        ids |= {r['subscriber_open_id'] for r in subscriptions}
        directory = self._identity_directory(ids)
        by_session = {}
        for row in subscriptions:  # newest first -> group then reverse per session
            by_session.setdefault(row['session_id'], []).append(row)
        items = []
        for s in sessions:
            owner = directory.get(s['owner_open_id']) or {}
            organizer = (directory.get(s.get('organizer_open_id')) or {}
                         if s.get('organizer_open_id') else {})
            diagnostics = None
            if s.get('drain_diagnostics'):
                try:
                    diagnostics = json.loads(s['drain_diagnostics'])
                except ValueError:
                    diagnostics = None
            subs = [{'request_no': r['request_no'],
                     'subscriber_open_id': r['subscriber_open_id'],
                     'state': r['state'],
                     'subscriber_name': (directory.get(r['subscriber_open_id']) or {}).get('name'),
                     'subscriber_email': (directory.get(r['subscriber_open_id']) or {}).get('email')}
                    for r in reversed(by_session.get(s['session_id'], []))]
            items.append({'session_id': s['session_id'],
                          'owner_open_id': s['owner_open_id'],
                          'owner_name': owner.get('name'),
                          'owner_email': owner.get('email'),
                          'organizer_open_id': s.get('organizer_open_id'),
                          'organizer_name': organizer.get('name'),
                          'organizer_email': organizer.get('email'),
                          'meeting_no': s['meeting_no'], 'title': s['title'],
                          'phase': s['phase'], 'started_at': s['started_at'],
                          'ended_at': s['ended_ns'] / 1e9 if s['ended_ns'] else None,
                          'drain_deadline_at': s.get('drain_deadline_at'),
                          'drain_forced': bool(diagnostics),
                          'drain_forced_reason': (diagnostics or {}).get('reason'),
                          'nsdp_hold': bool(s['nsdp_hold']),
                          'subscriptions': subs})
        return {'sessions': items}

    def admin_end_session(self, session_id):
        """Ops stop: owner check and NSDP gate intentionally bypassed."""
        try:
            session = self.store.require_session(session_id)
        except StorageError:
            raise ApiError(404, 'not_found', 'unknown session') from None
        if session['phase'] in ('draining', 'ended'):
            return {'session_id': session_id, 'phase': session['phase']}
        updated = self.store.set_phase(session_id, 'draining', now=self.clock(),
                                       drain_deadline_at=self.clock() + self.drain_deadline_s)
        assert updated is not None
        LOG.info('admin ending: session=%s -> draining', session_id)
        return {'session_id': session_id, 'phase': updated['phase']}

    def admin_session_transcript(self, session_id):
        """Ops export (§9.6): merged caption + LLM reply text timeline."""
        try:
            session = self.store.require_session(session_id)
        except StorageError:
            raise ApiError(404, 'not_found', 'unknown session') from None
        return {'session_id': session_id, 'meeting_no': session['meeting_no'],
                'title': session['title'],
                'records': self.store.session_transcript(session_id)}

    def admin_session_timings(self, session_id):
        """Ops export (§9.7): per-task stage timings for this session."""
        try:
            session = self.store.require_session(session_id)
        except StorageError:
            raise ApiError(404, 'not_found', 'unknown session') from None
        return {'session_id': session_id, 'meeting_no': session['meeting_no'],
                'title': session['title'],
                'jobs': self.store.session_job_timings(session_id)}

    # -- subscriptions --------------------------------------------------------

    def create_subscription(self, body):
        subscriber = body.get('subscriber_open_id')
        meeting_no = body.get('meeting_no')
        if not isinstance(subscriber, str) or not subscriber \
                or not isinstance(meeting_no, str):
            raise ApiError(400, 'bad_message', 'subscriber_open_id and meeting_no required')
        targets = self.store.active_sessions_by_meeting_no(meeting_no)
        if not targets:
            raise ApiError(404, 'not_found', 'meeting not currently served')
        if len(targets) > 1:
            raise ApiError(409, 'meeting_ambiguous',
                           'multiple active sessions share this meeting number')
        target = targets[0]
        try:
            result = self.store.create_subscription_request(
                target['session_id'], subscriber, now=self.clock(),
                approval_ttl_s=self.approval_ttl_s)
            if not result['existing']:
                profile = self._identity_directory({subscriber}).get(subscriber) or {}
                result['subscriber_display'] = (profile.get('user_id') or profile.get('name')
                                                or subscriber)
            return result
        except StateError as exc:
            failure = _state_error(exc)
            if failure.code == 'claim_conflict':
                failure.extra['conflicting_title'] = self.store.claimed_session_title(subscriber)
            raise failure from None
        except StorageError:
            raise ApiError(500, 'internal', 'storage') from None

    def decide_subscription(self, request_no, body):
        owner = body.get('owner_open_id')
        if isinstance(owner, str) and owner:
            self._gate_nsdp(owner)
        try:
            result = self.store.decide_subscription(
                request_no, owner, body.get('decision'),
                now=self.clock())
            if result.get('expired'):
                raise ApiError(409, 'invalid_state', 'approval deadline expired')
            return result
        except StateError as exc:
            raise _state_error(exc) from None
        except StorageError:
            raise ApiError(404, 'not_found', 'unknown request') from None

    def revoke_subscription(self, request_no, body):
        caller = body.get('owner_open_id') or body.get('subscriber_open_id')
        if body.get('owner_open_id'):
            self._gate_nsdp(body.get('owner_open_id'))
        try:
            return self.store.revoke_subscription(
                request_no, caller, now=self.clock())
        except StateError as exc:
            raise _state_error(exc) from None
        except StorageError:
            raise ApiError(404, 'not_found', 'unknown request') from None

    # -- outbox ---------------------------------------------------------------

    def list_subscriptions(self, subscriber):
        if not isinstance(subscriber, str) or not subscriber:
            raise ApiError(400, 'bad_message', 'subscriber_open_id required')
        return {'subscriptions': self.store.subscriptions_for_subscriber(subscriber)}

    def pending_outbox(self, limit):
        try:
            limit = max(1, min(50, int(limit)))
        except (TypeError, ValueError):
            limit = 10
        rows = self.store.pending_im_rows(limit=limit)
        return {'rows': [{'uuid': r['uuid'], 'kind': r['kind'],
                          'channel': r['channel'],
                          'recipient_open_id': r['recipient_id'],
                          'session_id': r['session_id'],
                          'payload': {'text': r['content']},
                          'created_at': r['created_ns'],
                          'attempts': r['attempts']} for r in rows]}

    def outbox_result(self, uuid, body):
        status = body.get('status')
        if status not in ('sending', 'sent', 'uncertain'):
            raise ApiError(400, 'bad_message', 'status must be sending|sent|uncertain')
        row = self.store.db.execute(
            'SELECT uuid,state,session_id FROM outbox WHERE uuid=?', (uuid,)).fetchone()
        if row is None:
            raise ApiError(404, 'not_found', 'unknown outbox row')
        assert row is not None
        if status == 'sending':
            # Two-phase send (v2 §1.3): claim before touching Feishu so a
            # crashed sender lands in recoverable `sending`, never a blind resend.
            if row['state'] == 'pending':
                self.store.mark_sending(uuid)
            return {'uuid': uuid, 'state': 'acknowledged'}
        if row['state'] == 'pending':
            self.store.mark_sending(uuid)
        if row['state'] in ('pending', 'sending'):
            if status == 'sent':
                self.store.mark_sent(uuid, now=self.clock(), interval=0)
            else:
                self.store.mark_send_error(uuid, body.get('error') or 'uncertain',
                                           uncertain=True)
            LOG.info('outbox result: uuid=%s status=%s session=%s', uuid, status,
                     row['session_id'])
        return {'uuid': uuid, 'state': 'acknowledged'}

    # -- GUI face (CONTRACT_ADVISOR_API §8) -------------------------------------

    def gui_login_start(self):
        try:
            started = self.auth.gui_authorize_start()
        except AuthUnavailable:
            raise ApiError(409, 'auth_unavailable', 'auth node unreachable') from None
        device_code = 'dc-' + secrets.token_urlsafe(24)
        expires_at = started.get('expires_at')
        if not isinstance(expires_at, (int, float)) or expires_at <= self.clock():
            expires_at = self.clock() + 600
        with self._gui_lock:
            self._prune_gui_logins()
            self._gui_logins[device_code] = {'state': started['state'],
                                             'expires_at': expires_at,
                                             'consumed': False}
        LOG.info('gui login started: device_code=%s…', device_code[:6])
        return {'login_url': started['authorize_url'], 'device_code': device_code,
                'expires_at': expires_at}

    def _prune_gui_logins(self):
        now = self.clock()
        for code in [c for c, e in self._gui_logins.items()
                     if e.get('consumed') or e['expires_at'] <= now]:
            del self._gui_logins[code]

    def gui_login_poll(self, device_code):
        if not isinstance(device_code, str) or not device_code.startswith('dc-') \
                or len(device_code) > 128:
            raise ApiError(404, 'unknown_device', 'unknown device code')
        with self._gui_lock:
            entry = self._gui_logins.get(device_code)
            if entry is not None and entry.get('consumed'):
                del self._gui_logins[device_code]
                entry = None
        if entry is None:
            raise ApiError(404, 'unknown_device', 'unknown or consumed device code')
        if self.clock() >= entry['expires_at']:
            with self._gui_lock:
                self._gui_logins.pop(device_code, None)
            raise ApiError(410, 'expired', 'device code expired; scan a new QR')
        try:
            status = self.auth.authorize_status(entry['state'])
        except AuthUnavailable:
            raise ApiError(409, 'auth_unavailable', 'auth node unreachable') from None
        if status.get('status') == 'expired':
            with self._gui_lock:
                self._gui_logins.pop(device_code, None)
            raise ApiError(410, 'expired', 'login state expired; scan a new QR')
        if status.get('status') != 'completed':
            return {'status': 'pending'}
        user = status.get('user') or {}
        open_id = user.get('open_id')
        if not isinstance(open_id, str) or not open_id:
            raise ApiError(410, 'expired', 'login completed without identity')
        device_token = 'dt-' + secrets.token_urlsafe(32)
        self.store.register_device(device_token, open_id, name=user.get('name'),
                                   email=user.get('email'), now=self.clock())
        self.store.upsert_user(open_id, name=user.get('name'),
                               en_name=user.get('en_name'), email=user.get('email'),
                               user_id=user.get('user_id'), now=self.clock())
        with self._gui_lock:
            self._gui_logins.pop(device_code, None)  # one-shot: second poll 404s
        LOG.info('gui device registered: open_id=%s', open_id)
        return {'status': 'completed', 'device_token': device_token,
                'user': {'open_id': open_id, 'name': user.get('name'),
                         'email': user.get('email'), 'user_id': user.get('user_id')}}

    def gui_sessions(self, token):
        device = self.store.device(token, now=self.clock())
        if device is None:
            raise ApiError(401, 'bad_token', 'unknown device token')
        sessions = self.store.list_sessions(device['open_id'])
        items = []
        for s in sessions:
            args = None
            if s['phase'] == 'running':
                token_row = self.store.session_token(s['session_id'])
                assert token_row is not None
                args = {'session': s['session_id'],
                        'event': f"{s['event_calendar_id']}/{s['event_id']}",
                        'url': self.ingest_url, 'token': token_row['stream_token'],
                        'mic': False}
            items.append({'session_id': s['session_id'], 'meeting_no': s['meeting_no'],
                          'title': s['title'], 'phase': s['phase'],
                          'started_at': s['started_at'], 'recorder_command_args': args})
        return {'user': {'open_id': device['open_id'], 'name': device['user_name'],
                         'email': device['user_email']}, 'sessions': items}

    def gui_logout(self, token):
        if self.store.device(token, now=self.clock()) is not None:
            self.store.drop_device(token)
            LOG.info('gui device removed')
        return {'ok': True}


def build_api_server(api: AdvisorApi, host='127.0.0.1', port=8380):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _reply(self, status, payload):
            body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass

        def _read_json(self):
            raw_length = self.headers.get('Content-Length')
            if raw_length is None:
                raise ApiError(400, 'bad_message', 'body required')
            try:
                length = int(raw_length)
            except ValueError:
                raise ApiError(400, 'bad_message', 'bad content length') from None
            if not 0 <= length <= 65536:
                raise ApiError(400, 'bad_message', 'body too large')
            try:
                value = json.loads(self.rfile.read(length))
            except (ValueError, UnicodeDecodeError):
                raise ApiError(400, 'bad_message', 'invalid json') from None
            if not isinstance(value, dict):
                raise ApiError(400, 'bad_message', 'object body required')
            return value

        def _bearer_token(self):
            value = self.headers.get('Authorization', '')
            if not value.startswith('Bearer '):
                raise ApiError(401, 'bad_token', 'bearer device token required')
            return value[len('Bearer '):].strip()

        def _run(self, action):
            try:
                status = 200
                payload, replay = action()
                if isinstance(payload, tuple):
                    payload, replay = payload
                if replay:
                    status = 200
                path = self.path.split('?')[0]
                quiet = (self.command == 'GET'
                         and (path == '/healthz' or path == '/outbox/pending'
                              or path == '/gui/login/poll' or path == '/gui/sessions'
                              or path == '/admin/sessions'))
                if not quiet:
                    LOG.info('api %s %s -> %s', self.command, path, status)
                self._reply(status, payload)
            except ApiError as exc:
                LOG.warning('api error %s %s -> %s %s %s', self.command, self.path,
                            exc.status, exc.code, exc.message)
                self._reply(exc.status, {'error': {'code': exc.code,
                                                    'message': exc.message} | exc.extra})
            except StorageError:
                self._reply(500, {'error': {'code': 'internal', 'message': 'storage'}})

        def do_GET(self):
            parsed = parse.urlsplit(self.path)
            query = parse.parse_qs(parsed.query, keep_blank_values=True)
            if parsed.path == '/healthz':
                self._reply(200, {'status': 'ok'})
            elif parsed.path == '/gui/login/poll':
                device_code = query.get('device_code', [None])[0]
                self._run(lambda: (api.gui_login_poll(device_code), False))
            elif parsed.path == '/gui/sessions':
                self._run(lambda: (api.gui_sessions(self._bearer_token()), False))
            elif parsed.path == '/meetings':
                owner = query.get('owner_open_id', [None])[0]
                self._run(lambda: (api.list_meetings(owner), False))
            elif parsed.path == '/sessions':
                self._run(lambda: (api.list_sessions(query.get('owner_open_id', [None])[0]), False))
            elif parsed.path == '/admin/sessions':
                self._run(lambda: (api.admin_sessions(), False))
            elif parsed.path == '/admin/supervisors':
                self._run(lambda: (api.admin_supervisors(), False))
            elif parsed.path == '/admin/config':
                self._run(lambda: (api.admin_get_config(), False))
            elif parsed.path.startswith('/sessions/') and parsed.path.endswith('/status'):
                session_id = parsed.path[len('/sessions/'):-len('/status')]
                self._run(lambda: (api.session_status(session_id), False))
            elif parsed.path.startswith('/admin/sessions/') and parsed.path.endswith('/transcript'):
                session_id = parsed.path[len('/admin/sessions/'):-len('/transcript')]
                self._run(lambda: (api.admin_session_transcript(session_id), False))
            elif parsed.path.startswith('/admin/sessions/') and parsed.path.endswith('/timings'):
                session_id = parsed.path[len('/admin/sessions/'):-len('/timings')]
                self._run(lambda: (api.admin_session_timings(session_id), False))
            elif parsed.path == '/subscriptions':
                subscriber = query.get('subscriber_open_id', [None])[0]
                self._run(lambda: (api.list_subscriptions(subscriber), False))
            elif parsed.path == '/outbox/pending':
                limit = query.get('limit', ['10'])[0]
                self._run(lambda: (api.pending_outbox(limit), False))
            else:
                self._reply(404, {'error': {'code': 'not_found', 'message': 'unknown path'}})

        def do_POST(self):
            parsed = parse.urlsplit(self.path)
            key = self.headers.get('X-Idempotency-Key')

            if parsed.path == '/gui/login/start':
                self._run(lambda: (api.gui_login_start(), False))  # 201-ish; keep 200
                return
            if parsed.path == '/gui/logout':
                self._run(lambda: (api.gui_logout(self._bearer_token()), False))
                return
            if parsed.path.startswith('/admin/sessions/') and parsed.path.endswith('/end'):
                session_id = parsed.path[len('/admin/sessions/'):-len('/end')]
                self._run(lambda: (api.admin_end_session(session_id), False))
                return
            if parsed.path == '/admin/supervisors':
                body = self._read_json()
                self._run(lambda: api.with_idempotency(
                    key, lambda: api.admin_put_supervisor(body)))
                return
            if parsed.path == '/admin/config':
                body = self._read_json()
                self._run(lambda: api.with_idempotency(
                    key, lambda: api.admin_put_config(body)))
                return

            def run(body):
                if parsed.path == '/sessions':
                    return api.with_idempotency(key, lambda: api.create_session(body))
                if parsed.path.startswith('/sessions/') and parsed.path.endswith('/end'):
                    session_id = parsed.path[len('/sessions/'):-len('/end')]
                    return api.end_session(session_id, body), False
                if parsed.path.startswith('/subscription-requests') and parsed.path.endswith('/decide'):
                    request_no = parsed.path[len('/subscription-requests/'):-len('/decide')]
                    try:
                        number = int(request_no)
                    except ValueError:
                        raise ApiError(404, 'not_found', 'bad request number') from None
                    return api.with_idempotency(key, lambda: api.decide_subscription(number, body))
                if parsed.path.startswith('/subscriptions/') and parsed.path.endswith('/revoke'):
                    request_no = parsed.path[len('/subscriptions/'):-len('/revoke')]
                    try:
                        number = int(request_no)
                    except ValueError:
                        raise ApiError(404, 'not_found', 'bad request number') from None
                    return api.with_idempotency(key, lambda: api.revoke_subscription(number, body))
                if parsed.path == '/subscription-requests':
                    return api.with_idempotency(key, lambda: api.create_subscription(body))
                if parsed.path.startswith('/outbox/') and parsed.path.endswith('/result'):
                    uuid = parsed.path[len('/outbox/'):-len('/result')]
                    return api.outbox_result(uuid, body), False
                raise ApiError(404, 'not_found', 'unknown path')

            self._run(lambda: run(self._read_json()))

    class Server(ThreadingHTTPServer):
        daemon_threads = True

        def handle_error(self, *args):
            pass

    return Server((host, port), Handler)
