"""Plan-1.4 acceptance (API surface): CONTRACT_ADVISOR_API over HTTP with
injected auth/calendar stubs."""
import json
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib import error, parse, request

from advisor.api import AdvisorApi, CalendarError, StubCalendarValidator, build_api_server
from advisor.auth_client import AuthorizationRequired, AuthUnavailable
from advisor.models import Caption
from advisor.store import SessionStore

OPENER = request.build_opener(request.ProxyHandler({}))


class FakeAuth:
    def __init__(self, mode='ok'):
        self.mode = mode
        self.profiles = {'ou_A': {'open_id': 'ou_A', 'name': 'Kai Han',
                                  'en_name': 'Kai Han', 'email': 'kai.han@nio.com'}}

    def get_token(self, open_id, *, rejected_token=None):
        if self.mode == 'missing':
            raise AuthorizationRequired('https://auth.internal/oauth/start?state=x')
        return {'access_token': 'u-x', 'expires_at': time.time() + 3600, 'scopes': []}

    def authorize_url(self, open_id, intent=''):
        return f'https://auth.internal/oauth/start?intent={intent}'

    def get_nsdp_token(self, open_id):
        return {'token': 'nsdp-token'}

    def get_jira_token(self, open_id):
        return {'token': 'jira-token'}

    def user_profile(self, open_id):
        if self.mode == 'auth_down':
            raise AuthUnavailable('test')
        profile = self.profiles.get(open_id)
        return dict(profile) if profile else None


class FakeCalendar(StubCalendarValidator):
    def __init__(self, code=None, description='日历描述'):
        super().__init__(description)
        self.code = code

    def validate(self, owner_open_id, meeting_ref):
        if self.code:
            raise CalendarError(self.code)
        return super().validate(owner_open_id, meeting_ref)


class SupervisorSpy:
    def __init__(self):
        self.started = []

    def start_session(self, session_id):
        self.started.append(session_id)


class ApiBase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'adviser.sqlite3'
        SessionStore.initialize(self.path)
        self.store = SessionStore(self.path)
        self.addCleanup(self.store.close)
        self.auth = FakeAuth()
        self.calendar = FakeCalendar()
        self.spy = SupervisorSpy()
        self.api = AdvisorApi(self.store, self.spy, auth_client=self.auth,
                              calendar_validator=self.calendar,
                              ingest_url='ws://127.0.0.1:8400/ingest')
        self.server = build_api_server(self.api, host='127.0.0.1', port=0)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.counter = 0

    def url(self, path):
        return f'http://127.0.0.1:{self.port}{path}'

    def get(self, path):
        with OPENER.open(request.Request(self.url(path), method='GET'), timeout=5) as response:
            return response.status, json.loads(response.read())

    def get_error(self, path):
        try:
            self.get(path)
            self.fail('expected HTTPError')
        except error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def post(self, path, body, key=None):
        self.counter += 1
        headers = {'Content-Type': 'application/json'}
        if key is not None:
            headers['X-Idempotency-Key'] = key
        req = request.Request(self.url(path), data=json.dumps(body).encode(),
                              method='POST', headers=headers)
        with OPENER.open(req, timeout=5) as response:
            return response.status, json.loads(response.read())

    def post_error(self, path, body, key=None):
        try:
            self.post(path, body, key)
            self.fail('expected HTTPError')
        except error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def join(self, *, key=None, owner='ou_A', no='123456789', mic=False):
        return self.post('/sessions', {'owner_open_id': owner,
                                       'meeting_ref': {'no': no}, 'mic_enabled': mic},
                         key=key or f'join-{self.counter}-{no}')


class ClaimConflictMessageTests(ApiBase):
    def test_join_conflict_carries_existing_meeting_title(self):
        self.join()
        status, body = self.post_error('/sessions',
                                       {'owner_open_id': 'ou_A',
                                        'meeting_ref': {'no': '987654321'}}, key='other-meeting')
        self.assertEqual((status, body['error']['code']), (409, 'claim_conflict'))
        self.assertEqual(body['error']['conflicting_title'], '会议 123456789')

    def test_subscribe_conflict_carries_existing_meeting_title(self):
        self.join(owner='ou_B', no='987654321')
        self.join()
        status, body = self.post_error('/subscription-requests',
                                       {'subscriber_open_id': 'ou_B',
                                        'meeting_no': '123456789'}, key='sub-other')
        self.assertEqual((status, body['error']['code']), (409, 'claim_conflict'))
        self.assertEqual(body['error']['conflicting_title'], '会议 987654321')


class CAdmissionTests(ApiBase):
    """SM-04: supervisor (小C) admission via grant list + occurrence invite."""

    def setUp(self):
        super().setUp()
        self.calendar.occurrences = {'123456789': [
            {'start_ts': 1762800000, 'organizer_open_id': 'ou_O',
             'attendees': ['ou_C', 'ou_B']}]}

    def grant(self, open_id, enabled=True):
        self.store.put_supervisor_grant(f'g-{open_id}', open_id=open_id,
                                        enabled=enabled, now=1.0)

    def test_invited_supervisor_creates_with_own_identity(self):
        self.grant('ou_C')
        status, body = self.join(owner='ou_C')
        self.assertEqual(status, 200)
        self.assertEqual(body['phase'], 'running')
        row = self.store.require_session(body['session_id'])
        self.assertEqual(row['owner_open_id'], 'ou_C')
        self.assertEqual(row['organizer_open_id'], 'ou_O')

    def test_organizer_path_unaffected_by_grants(self):
        status, body = self.join(owner='ou_O')
        self.assertEqual(status, 200)
        self.assertEqual(body['phase'], 'running')

    def test_plain_participant_without_grant_rejected(self):
        status, body = self.post_error('/sessions',
                                       {'owner_open_id': 'ou_B',
                                        'meeting_ref': {'no': '123456789'}},
                                       key='c-nogrant')
        self.assertEqual((status, body['error']['code']), (409, 'not_supervisor'))

    def test_granted_but_not_invited_rejected(self):
        self.calendar.occurrences['123456789'][0]['attendees'] = ['ou_X']
        self.grant('ou_C')
        status, body = self.post_error('/sessions',
                                       {'owner_open_id': 'ou_C',
                                        'meeting_ref': {'no': '123456789'}},
                                       key='c-noinvite')
        self.assertEqual((status, body['error']['code']), (409, 'not_invited'))

    def test_disabled_grant_rejected(self):
        self.grant('ou_C', enabled=False)
        status, body = self.post_error('/sessions',
                                       {'owner_open_id': 'ou_C',
                                        'meeting_ref': {'no': '123456789'}},
                                       key='c-disabled')
        self.assertEqual((status, body['error']['code']), (409, 'not_supervisor'))

    def test_supervisor_organizer_uses_a_path(self):
        # C 在另一场自己是组织者：organizer_ok 命中，无需名单
        self.calendar.occurrences['999888777'] = [
            {'start_ts': 1762800000, 'organizer_open_id': 'ou_C', 'attendees': []}]
        status, body = self.post('/sessions', {'owner_open_id': 'ou_C',
                                               'meeting_ref': {'no': '999888777'}},
                                 key='c-organizer')
        self.assertEqual(status, 200)

    # -- v2.6 (2026-11-19 bug): invite proof via an invited chat group --------

    def group_occurrence(self, *, members=frozenset()):
        self.calendar.occurrences['123456789'] = [
            {'start_ts': 1762800000, 'organizer_open_id': 'ou_O',
             'attendees': ['ou_X'],
             'chat_attendee_ids': ['chat_1'],
             'chat_members': {'chat_1': set(members)}}]

    def test_group_member_supervisor_admitted(self):
        # 小C 不在直接邀请名单，而是受邀群组成员：经群成员展开命中
        self.group_occurrence(members={'ou_C', 'ou_OTHER'})
        self.grant('ou_C')
        status, body = self.join(owner='ou_C', key='c-group')
        self.assertEqual(status, 200)
        self.assertEqual(body['phase'], 'running')
        row = self.store.require_session(body['session_id'])
        self.assertEqual(row['owner_open_id'], 'ou_C')

    def test_group_member_without_grant_rejected(self):
        self.group_occurrence(members={'ou_B'})
        status, body = self.post_error('/sessions',
                                       {'owner_open_id': 'ou_B',
                                        'meeting_ref': {'no': '123456789'}},
                                       key='c-group-nogrant')
        self.assertEqual((status, body['error']['code']), (409, 'not_supervisor'))

    def test_granted_outside_invited_group_rejected(self):
        self.group_occurrence(members={'ou_OTHER'})
        self.grant('ou_C')
        status, body = self.post_error('/sessions',
                                       {'owner_open_id': 'ou_C',
                                        'meeting_ref': {'no': '123456789'}},
                                       key='c-outside-group')
        self.assertEqual((status, body['error']['code']), (409, 'not_invited'))

    def test_chat_expansion_failure_fails_closed(self):
        self.group_occurrence(members={'ou_OTHER'})
        self.grant('ou_C')

        def boom(owner, binding, caller_ids):
            raise CalendarError('auth_unavailable')
        self.calendar.chat_member_hit = boom
        status, body = self.post_error('/sessions',
                                       {'owner_open_id': 'ou_C',
                                        'meeting_ref': {'no': '123456789'}},
                                       key='c-group-down')
        self.assertEqual((status, body['error']['code']), (409, 'auth_unavailable'))


class CreateSessionTests(ApiBase):
    def test_all_missing_credentials_are_reported_before_reusing_or_creating_session(self):
        self.auth.mode = 'missing'
        def missing(provider):
            def check(owner):
                raise AuthorizationRequired(f'https://auth.internal/{provider}/submit?state=x', provider)
            return check
        self.auth.get_nsdp_token = missing('nsdp')
        self.auth.get_jira_token = missing('jira')
        status, body = self.post_error('/sessions', {'owner_open_id': 'ou_A',
                                                     'meeting_ref': {'no': '123456789'}}, key='all-missing')
        self.assertEqual(status, 409)
        missing_items = body['error']['missing_credentials']
        self.assertEqual([item['provider'] for item in missing_items], ['feishu', 'nsdp', 'jira'])
        self.assertEqual(self.spy.started, [])
        self.assertEqual(self.store.list_sessions('ou_A'), [])

    def test_auth_unavailable_blocks_join(self):
        def unavailable(owner):
            raise AuthUnavailable('network')
        self.auth.get_jira_token = unavailable
        status, body = self.post_error('/sessions', {'owner_open_id': 'ou_A',
                                                     'meeting_ref': {'no': '123456789'}}, key='down')
        self.assertEqual((status, body['error']['code']), (409, 'auth_unavailable'))
        self.assertEqual(self.spy.started, [])

    def test_reuse_requires_credentials(self):
        self.join(key='original')
        def missing(owner):
            raise AuthorizationRequired('https://auth.internal/jira/submit?state=x', 'jira')
        self.auth.get_jira_token = missing
        status, body = self.post_error('/sessions', {'owner_open_id': 'ou_A',
                                                     'meeting_ref': {'no': '123456789'}}, key='retry-new')
        self.assertEqual((status, body['error']['provider']), (409, 'jira'))
        self.assertEqual(len(self.spy.started), 1)

    def test_happy_path_returns_command_args_and_starts_supervisor(self):
        status, body = self.join()
        self.assertEqual(status, 200)
        self.assertEqual(body['phase'], 'running')
        self.assertFalse(body['existing'])
        args = body['recorder_command_args']
        self.assertEqual(args['session'], body['session_id'])
        self.assertEqual(args['event'], 'cal_stub/evt_123456789')
        self.assertEqual(args['url'], 'ws://127.0.0.1:8400/ingest')
        self.assertTrue(args['token'].startswith('st-'))
        self.assertEqual(self.spy.started, [body['session_id']])
        record = self.store.materials(body['session_id'])
        self.assertEqual(record['description'], '日历描述')

    def test_authorization_required_carries_url(self):
        self.auth.mode = 'missing'
        status, body = self.post_error('/sessions', {'owner_open_id': 'ou_A',
                                                     'meeting_ref': {'no': '123456789'}},
                                       key='auth-1')
        self.assertEqual(status, 409)
        self.assertEqual(body['error']['code'], 'authorization_required')
        self.assertIn('authorize_url', body['error'])
        self.assertIn('intent=join:123456789', body['error']['authorize_url'])

    def test_four_rejection_codes_map(self):
        for code in ('meeting_not_found', 'outside_window'):
            self.calendar.code = code
            status, body = self.post_error('/sessions', {'owner_open_id': 'ou_A',
                                                         'meeting_ref': {'no': '123456789'}},
                                           key=f'rej-{code}')
            self.assertEqual((status, body['error']['code']), (409, code))
        status, body = self.post_error('/sessions', {'owner_open_id': 'x', 'meeting_ref': {}},
                                       key='rej-bad')
        self.assertEqual((status, body['error']['code']), (400, 'bad_message'))

    def test_duplicate_join_is_idempotent_on_both_levels(self):
        _, first = self.join(key='join-fixed')
        _, again = self.join(key='join-fixed')  # idempotency replay
        self.assertEqual(first['session_id'], again['session_id'])
        self.assertEqual(self.spy.started, [first['session_id']])
        _, third = self.join(key='join-other')  # new key, same active meeting
        self.assertTrue(third['existing'])
        self.assertEqual(third['session_id'], first['session_id'])
        self.assertEqual(self.spy.started, [first['session_id']])


class SessionOpsTests(ApiBase):
    def created(self):
        _, body = self.join()
        return body

    def test_list_and_status(self):
        body = self.created()
        status, listing = self.get(f"/sessions?owner_open_id=ou_A")
        self.assertEqual([s['session_id'] for s in listing['sessions']], [body['session_id']])
        status, state = self.get(f"/sessions/{body['session_id']}/status")
        self.assertEqual(state['phase'], 'running')
        self.assertEqual(state['material_present'], True)

    def test_end_owner_only_and_idempotent(self):
        body = self.created()
        status, result = self.post_error(f"/sessions/{body['session_id']}/end",
                                         {'owner_open_id': 'ou_B'}, key='end-b')
        self.assertEqual((status, result['error']['code']), (409, 'not_owner'))
        status, result = self.post(f"/sessions/{body['session_id']}/end",
                                   {'owner_open_id': 'ou_A'}, key='end-a')
        self.assertEqual(result['phase'], 'draining')
        self.store.set_phase(body['session_id'], 'ended')
        status, result = self.post(f"/sessions/{body['session_id']}/end",
                                   {'owner_open_id': 'ou_A'}, key='end-a2')
        self.assertEqual(result['phase'], 'ended')

    def test_unknown_session_404(self):
        status, body = self.post_error('/sessions/s-nope/end', {'owner_open_id': 'ou_A'}, key='x')
        self.assertEqual((status, body['error']['code']), (404, 'not_found'))


class AdminFaceTests(ApiBase):
    def created(self, owner='ou_A', no='123456789'):
        _, body = self.join(owner=owner, no=no)
        return body

    def subscribe(self, session, subscriber='ou_B'):
        return self.post('/subscription-requests',
                         {'subscriber_open_id': subscriber,
                          'meeting_no': session['meeting_no']},
                         key=f'admin-sub-{self.counter}')

    def test_admin_sessions_list_all_owners_and_ended(self):
        first = self.created(owner='ou_A', no='111111111')
        second = self.created(owner='ou_B', no='222222222')
        self.store.set_phase(first['session_id'], 'draining')
        self.store.set_phase(first['session_id'], 'ended')
        status, listing = self.get('/admin/sessions')
        self.assertEqual(status, 200)
        rows = {s['session_id']: s for s in listing['sessions']}
        self.assertEqual(set(rows), {first['session_id'], second['session_id']})
        self.assertEqual(rows[first['session_id']]['phase'], 'ended')
        self.assertEqual(rows[first['session_id']]['owner_open_id'], 'ou_A')
        self.assertIsNotNone(rows[first['session_id']]['ended_at'])
        self.assertIsNone(rows[second['session_id']]['ended_at'])
        self.assertEqual(rows[second['session_id']]['owner_open_id'], 'ou_B')
        self.assertFalse(rows[second['session_id']]['nsdp_hold'])
        # identity directory: ou_A has an auth profile, ou_B never authorized
        self.assertEqual(rows[first['session_id']]['owner_name'], 'Kai Han')
        self.assertEqual(rows[first['session_id']]['owner_email'], 'kai.han@nio.com')
        self.assertIsNone(rows[second['session_id']]['owner_name'])
        self.assertEqual(rows[first['session_id']]['subscriptions'], [])
        # ordering: newest started_at first
        self.assertEqual(listing['sessions'][0]['session_id'], second['session_id'])

    def test_admin_sessions_include_subscribers_with_directory(self):
        session = self.created(owner='ou_A', no='333333333')
        _, sub = self.subscribe(session)
        self.post(f"/subscription-requests/{sub['request_no']}/decide",
                  {'owner_open_id': 'ou_A', 'decision': 'approve'}, key='decide-1')
        status, listing = self.get('/admin/sessions')
        row = listing['sessions'][0]
        self.assertEqual(len(row['subscriptions']), 1)
        entry = row['subscriptions'][0]
        self.assertEqual(entry['request_no'], sub['request_no'])
        self.assertEqual(entry['subscriber_open_id'], 'ou_B')
        self.assertEqual(entry['state'], 'approved')
        self.assertIsNone(entry['subscriber_name'])  # ou_B never authorized: open_id fallback
        # once ou_B shows up in the directory, later listings carry the name
        # (tombstone throttle: force the 5min re-check window to have passed)
        self.store.db.execute("UPDATE users SET checked_ns=0 WHERE open_id='ou_B'")
        self.auth.profiles['ou_B'] = {'open_id': 'ou_B', 'name': 'Bob Li',
                                      'en_name': 'Bob Li', 'email': 'bob@nio.com'}
        _, listing = self.get('/admin/sessions')
        entry = listing['sessions'][0]['subscriptions'][0]
        self.assertEqual(entry['subscriber_name'], 'Bob Li')
        self.assertEqual(entry['subscriber_email'], 'bob@nio.com')

    def test_admin_sessions_degrade_when_auth_down(self):
        self.created(owner='ou_C', no='444444444')
        self.auth.mode = 'auth_down'
        status, listing = self.get('/admin/sessions')
        self.assertEqual(status, 200)  # listing still works, open_id-only display
        self.assertIsNone(listing['sessions'][0]['owner_name'])

    def test_admin_end_without_body_or_owner(self):
        body = self.created()
        req = request.Request(self.url(f"/admin/sessions/{body['session_id']}/end"),
                              data=b'', method='POST')
        with OPENER.open(req, timeout=5) as response:
            self.assertEqual(response.status, 200)
            result = json.loads(response.read())
        self.assertEqual(result['phase'], 'draining')
        # idempotent: draining again stays draining, no body/key needed
        status, again = self.post(f"/admin/sessions/{body['session_id']}/end", {}, key=None)
        self.assertEqual(again['phase'], 'draining')
        self.store.set_phase(body['session_id'], 'ended')
        status, ended = self.post(f"/admin/sessions/{body['session_id']}/end", {}, key=None)
        self.assertEqual(ended['phase'], 'ended')

    def test_admin_end_unknown_404(self):
        status, body = self.post_error('/admin/sessions/s-nope/end', {}, key=None)
        self.assertEqual((status, body['error']['code']), (404, 'not_found'))

    def test_admin_transcript_merges_captions_and_replies(self):
        session = self.created()
        sid = session['session_id']
        self.store.ingest_caption(sid, Caption(
            meeting_id=sid, sentence_key='loopback:t1:0-100', text='我们过一下预算',
            received_at='2033-05-14T22:42:10+00:00',
            event_time=2000000010000, start_time=2000000007000,
            end_time=2000000010000), now=2_000_000_010.0)
        self.store.ingest_caption(sid, Caption(
            meeting_id=sid, sentence_key='mic:t2:0-100', text='好的，我补充',
            received_at='2033-05-14T22:42:20+00:00',
            event_time=2000000020000, start_time=2000000017000,
            end_time=2000000020000, speaker_name='本机'), now=2_000_000_020.0)
        with self.store.unit_of_work() as tx:
            tx.outbox.save_reply(message_uuid='uuid-1', session_id=sid,
                                 content='建议补充预算数据', evidence='{}', generation=3,
                                 created_ns=2_000_000_015_000_000_000,
                                 context_version=None, recipients=('ou_A',),
                                 now=2_000_000_015.0)
        status, transcript = self.get(f'/admin/sessions/{sid}/transcript')
        self.assertEqual(status, 200)
        self.assertEqual(transcript['session_id'], sid)
        self.assertEqual(transcript['meeting_no'], '123456789')
        self.assertEqual(transcript['title'], '会议 123456789')
        self.assertEqual([r['type'] for r in transcript['records']],
                         ['human', 'llm', 'human'])  # by ascending time
        first, reply, second = transcript['records']
        self.assertEqual((first['time'], first['speaker'], first['text']),
                         (2000000010.0, None, '我们过一下预算'))
        self.assertEqual((reply['time'], reply['speaker'], reply['text']),
                         (2000000015.0, 'LLM', '建议补充预算数据'))
        self.assertEqual((second['time'], second['speaker'], second['text']),
                         (2000000020.0, '本机', '好的，我补充'))

    def test_admin_transcript_dedupes_im_fanout_rows(self):
        session = self.created()
        sid = session['session_id']
        with self.store.unit_of_work() as tx:
            tx.outbox.save_reply(message_uuid='uuid-2', session_id=sid,
                                 content='回答', evidence='{}', generation=1,
                                 created_ns=2_000_000_010_000_000_000,
                                 context_version=None,
                                 recipients=('ou_A', 'ou_B', 'ou_C'),
                                 now=2_000_000_010.0)
        _, transcript = self.get(f'/admin/sessions/{sid}/transcript')
        self.assertEqual(len(transcript['records']), 1)  # local fact row only
        # owner notices ride channel=im and never enter the transcript
        self.store.insert_owner_notice(sid, 'ou_A', '请重新提交 NSDP token',
                                       now=2_000_000_011.0)
        _, transcript = self.get(f'/admin/sessions/{sid}/transcript')
        self.assertEqual(len(transcript['records']), 1)

    def test_admin_timings_export_lists_job_rows(self):
        session = self.created()
        sid = session['session_id']
        with self.store.unit_of_work() as tx:
            tx.connection.execute(
                "INSERT INTO jobs(job_id,session_id,generation,snapshot,role_version,"
                "state,created_ns,timings) VALUES ('job-1',?,1,'{}','v1','succeeded',1,?)",
                (sid, '{"execution_ms":5,"process_start_ms":1,"request_id":"req-0001"}'))
            tx.connection.execute(
                "INSERT INTO jobs(job_id,session_id,generation,snapshot,role_version,"
                "state,created_ns) VALUES ('job-2',?,2,'{}','v1','pending',2)", (sid,))
        status, body = self.get(f'/admin/sessions/{sid}/timings')
        self.assertEqual(status, 200)
        self.assertEqual(body['session_id'], sid)
        self.assertEqual([job['job_id'] for job in body['jobs']], ['job-1', 'job-2'])
        self.assertEqual(body['jobs'][0]['timings']['process_start_ms'], 1)
        self.assertEqual(body['jobs'][0]['timings']['request_id'], 'req-0001')
        self.assertIsNone(body['jobs'][1]['timings'])

    def test_admin_timings_unknown_session_404(self):
        status, body = self.get_error('/admin/sessions/s-nope/timings')
        self.assertEqual((status, body['error']['code']), (404, 'not_found'))

    def test_admin_transcript_unknown_404(self):
        status, body = self.get_error('/admin/sessions/s-nope/transcript')
        self.assertEqual((status, body['error']['code']), (404, 'not_found'))


class SubscriptionApiTests(ApiBase):
    def created(self):
        _, body = self.join()
        return body

    def subscribe(self, session, subscriber='ou_B', key=None):
        return self.post('/subscription-requests',
                         {'subscriber_open_id': subscriber,
                          'meeting_no': session['meeting_no']}, key=key or f'sub-{self.counter}')

    def test_full_subscription_lifecycle(self):
        session = self.created()
        status, request_row = self.subscribe(session)
        self.assertEqual((status, request_row['state']), (200, 'requested'))
        self.assertEqual(request_row['owner_open_id'], 'ou_A')
        status, decided = self.post(
            f"/subscription-requests/{request_row['request_no']}/decide",
            {'owner_open_id': 'ou_A', 'decision': 'approve'}, key='decide-1')
        self.assertEqual(decided['state'], 'approved')
        status, revoked = self.post(
            f"/subscriptions/{request_row['request_no']}/revoke",
            {'owner_open_id': 'ou_A'}, key='revoke-1')
        self.assertEqual(revoked['state'], 'owner_revoked')
        status, again = self.subscribe(session, key='sub-2')
        self.assertEqual((status, again['state']), (200, 'requested'))
        self.assertGreater(again['request_no'], request_row['request_no'])
        self.assertEqual(again['previous']['state'], 'owner_revoked')

    def test_late_approval_is_rejected_and_frees_claim(self):
        session = self.created()
        now = self.api.clock()
        self.api.approval_ttl_s = 1
        self.api.clock = lambda: now
        _, pending = self.subscribe(session)
        self.assertEqual(pending['approval_deadline_at'], now + 1)
        self.api.clock = lambda: now + 1
        status, body = self.post_error(
            f"/subscription-requests/{pending['request_no']}/decide",
            {'owner_open_id': 'ou_A', 'decision': 'approve'}, key='late-approval')
        self.assertEqual((status, body['error']['code']), (409, 'invalid_state'))
        self.assertIsNone(self.store.active_claim('ou_B'))

    def test_unknown_meeting_404(self):
        status, body = self.post_error('/subscription-requests',
                                       {'subscriber_open_id': 'ou_B',
                                        'meeting_no': '999999999'}, key='sub-x')
        self.assertEqual((status, body['error']['code']), (404, 'not_found'))


class MeetingsApiTests(ApiBase):
    def test_lists_meetings(self):
        status, body = self.get('/meetings?owner_open_id=ou_A')
        self.assertEqual(status, 200)
        self.assertEqual(body['meetings'][0]['meeting_no'], '123456789')

    def test_meetings_require_owner(self):
        try:
            self.get('/meetings')
            self.fail('expected HTTPError')
        except error.HTTPError as exc:
            self.assertEqual(exc.code, 400)

    def test_meetings_authorization_required(self):
        self.auth.mode = 'missing'
        try:
            self.get('/meetings?owner_open_id=ou_A')
            self.fail('expected HTTPError')
        except error.HTTPError as exc:
            self.assertEqual(exc.code, 409)
            payload = json.loads(exc.read())
            self.assertEqual(payload['error']['code'], 'authorization_required')


class OutboxApiTests(ApiBase):
    def test_pending_and_result_roundtrip(self):
        _, session = self.join()
        with self.store.unit_of_work() as tx:
            tx.outbox.save_reply(message_uuid='uuid-1', session_id=session['session_id'],
                                 content='回答文本', evidence='{}', generation=1,
                                 created_ns=int(time.time() * 1e9), context_version=None,
                                 recipients=('ou_A',), now=time.time())
        status, pending = self.get('/outbox/pending?limit=5')
        self.assertEqual(len(pending['rows']), 1)
        row = pending['rows'][0]
        self.assertEqual(row['recipient_open_id'], 'ou_A')
        self.assertEqual(row['payload']['text'], '回答文本')
        status, result = self.post(f"/outbox/{row['uuid']}/result",
                                   {'status': 'sent'}, key='send-1')
        self.assertEqual(result['state'], 'acknowledged')
        state = self.store.db.execute('SELECT state FROM outbox WHERE uuid=?',
                                      (row['uuid'],)).fetchone()['state']
        self.assertEqual(state, 'sent')
        status, empty = self.get('/outbox/pending?limit=5')
        self.assertEqual(empty['rows'], [])


if __name__ == '__main__':
    unittest.main()
