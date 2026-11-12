"""Advisor HTTP API on 127.0.0.1:8380 per CONTRACT_ADVISOR_API v1.

Ports (auth client, calendar validator) are injected; the calendar validator
interface returns the binding + title + description on success and raises
CalendarError(code) for the four S1 rejection paths.
"""
import json
import logging
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib import parse

from .auth_client import AuthorizationRequired, AuthUnavailable
from .errors import StateError, StorageError
from .nsdp import NsdpUnavailable
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


class StubCalendarValidator(CalendarValidator):
    """Approves everything with a fixed binding (stub-first stages only)."""

    def __init__(self, description=''):
        self.description = description

    def validate(self, owner_open_id, meeting_ref):
        return {'event_calendar_id': 'cal_stub', 'event_id': f'evt_{meeting_ref.get("no")}',
                'title': f'会议 {meeting_ref.get("no")}', 'description': self.description}

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
               'not_owner': (409, 'not_owner'), 'session_not_recording': (409, 'session_ended')}
    status, code = mapping.get(exc.args[0] if exc.args else '', (409, 'invalid_state'))
    return ApiError(status, code, 'state machine rejected the operation')


class AdvisorApi:
    """Domain glue between HTTP and store/supervisor; testable without HTTP."""

    def __init__(self, store: SessionStore, supervisor, *,
                 auth_client, calendar_validator, ingest_url, host='127.0.0.1',
                 clock=time.time, nsdp=None):
        self.store = store
        self.supervisor = supervisor
        self.auth = auth_client
        self.calendar = calendar_validator
        self.ingest_url = ingest_url.rstrip('/')
        self.host = host
        self.clock = clock
        self.nsdp = nsdp

    def _gate_nsdp(self, owner):
        """Organizer-surface commands require an NSDP token (hard gate).

        小B (subscriber surface) never passes through here.
        """
        if self.nsdp is None:
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

    def create_session(self, body):
        owner = body.get('owner_open_id')
        meeting_ref = body.get('meeting_ref') or {}
        meeting_no = meeting_ref.get('no')
        if not isinstance(owner, str) or not owner or not isinstance(meeting_no, str) \
                or not meeting_no.isdigit() or not 6 <= len(meeting_no) <= 12:
            raise ApiError(400, 'bad_message', 'owner_open_id and meeting_ref.no required')

        active = self.store.db.execute(
            "SELECT * FROM sessions WHERE owner_open_id=? AND meeting_no=? "
            "AND phase IN ('recording','draining')", (owner, meeting_no)).fetchone()
        if active is not None:
            LOG.info('session reuse: session=%s meeting=%s owner=%s',
                     active['session_id'], meeting_no, owner)
            return self._session_response(dict(active), existing=True)

        self._gate_nsdp(owner)
        try:
            self.auth.get_token(owner)
        except AuthorizationRequired:
            url = self.auth.authorize_url(owner, f'join:{meeting_no}')
            raise ApiError(409, 'authorization_required', 'user token missing/expired',
                           authorize_url=url) from None
        except AuthUnavailable:
            raise ApiError(409, 'auth_unavailable', 'auth node unreachable') from None
        try:
            binding = self.calendar.validate(owner, meeting_ref)
        except CalendarError as exc:
            raise ApiError(409, exc.code, 'calendar validation failed') from None

        nsdp_text = ''
        if self.nsdp is not None:
            try:
                nsdp_text = self.nsdp.fetch(owner, binding.get('description') or '')
            except AuthorizationRequired as exc:
                raise ApiError(409, 'authorization_required', 'nsdp token missing/expired',
                               provider='nsdp', submit_url=exc.url) from None
            except AuthUnavailable:
                raise ApiError(409, 'auth_unavailable', 'auth node unreachable') from None
            except NsdpUnavailable:
                raise ApiError(409, 'nsdp_unavailable',
                               'NSDP unreachable; retry the command later') from None

        session = self.store.create_session(
            owner_open_id=owner, meeting_no=meeting_no,
            title=binding['title'], event_calendar_id=binding['event_calendar_id'],
            event_id=binding['event_id'], description=binding.get('description') or '',
            now=self.clock(), nsdp_text=nsdp_text)
        assert session is not None
        self.supervisor.start_session(session['session_id'])
        LOG.info('session created: session=%s meeting=%s owner=%s title="%s"',
                 session['session_id'], meeting_no, owner, session['title'])
        return self._session_response(session)

    def _session_response(self, session, existing=False):
        token_row = self.store.session_token(session['session_id'])
        assert token_row is not None
        return {
            'session_id': session['session_id'], 'meeting_no': session['meeting_no'],
            'title': session['title'], 'phase': session['phase'],
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
        if session['phase'] == 'ended':
            return {'session_id': session_id, 'phase': 'ended'}
        updated = self.store.set_phase(session_id, 'draining', now=self.clock())
        assert updated is not None
        LOG.info('session ending: session=%s -> draining (owner=%s)', session_id, owner)
        return {'session_id': session_id, 'phase': updated['phase']}

    # -- subscriptions --------------------------------------------------------

    def create_subscription(self, body):
        subscriber = body.get('subscriber_open_id')
        meeting_no = body.get('meeting_no')
        if not isinstance(subscriber, str) or not subscriber \
                or not isinstance(meeting_no, str):
            raise ApiError(400, 'bad_message', 'subscriber_open_id and meeting_no required')
        target = self.store.active_by_meeting_no(meeting_no)
        if target is None:
            raise ApiError(404, 'not_found', 'meeting not currently served')
        try:
            return self.store.create_subscription_request(
                target['session_id'], subscriber, now=self.clock())
        except StateError as exc:
            code = exc.args[0] if exc.args else 'invalid_state'
            if code == 'not_found':
                raise ApiError(404, 'not_found', 'meeting not currently served') from None
            raise ApiError(409, 'invalid_state',
                           'subscription revoked by organizer; terminal state') from None
        except StorageError:
            raise ApiError(500, 'internal', 'storage') from None

    def decide_subscription(self, request_no, body):
        owner = body.get('owner_open_id')
        if isinstance(owner, str) and owner:
            self._gate_nsdp(owner)
        try:
            return self.store.decide_subscription(
                request_no, owner, body.get('decision'),
                now=self.clock())
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
        if status not in ('sent', 'uncertain'):
            raise ApiError(400, 'bad_message', 'status must be sent|uncertain')
        row = self.store.db.execute(
            'SELECT uuid,state,session_id FROM outbox WHERE uuid=?', (uuid,)).fetchone()
        if row is None:
            raise ApiError(404, 'not_found', 'unknown outbox row')
        assert row is not None
        if row['state'] == 'pending':
            self.store.mark_sending(uuid)
            if status == 'sent':
                self.store.mark_sent(uuid, now=self.clock(), interval=0)
            else:
                self.store.mark_send_error(uuid, body.get('error') or 'uncertain',
                                           uncertain=True)
            LOG.info('outbox result: uuid=%s status=%s session=%s', uuid, status,
                     row['session_id'])
        return {'uuid': uuid, 'state': 'acknowledged'}


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
                         and (path == '/healthz' or path == '/outbox/pending'))
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
            elif parsed.path == '/meetings':
                owner = query.get('owner_open_id', [None])[0]
                self._run(lambda: (api.list_meetings(owner), False))
            elif parsed.path == '/sessions':
                self._run(lambda: (api.list_sessions(query.get('owner_open_id', [None])[0]), False))
            elif parsed.path.startswith('/sessions/') and parsed.path.endswith('/status'):
                session_id = parsed.path[len('/sessions/'):-len('/status')]
                self._run(lambda: (api.session_status(session_id), False))
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
