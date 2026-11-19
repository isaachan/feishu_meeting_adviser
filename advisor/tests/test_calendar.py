"""Real calendar validator unit tests: injected HTTP responses, all four S1
rejection paths, meeting_url matching, silent token refresh, material pickup,
recurring-series master/exception semantics (2026-11-18 bug)."""
import io
import json
import time
import unittest
from datetime import datetime
from urllib import error
from zoneinfo import ZoneInfo

from advisor.api import CalendarError
from advisor.auth_client import AuthorizationRequired
from advisor.calendar import FeishuCalendarValidator

NOW = 2_000_000_000
MEETING_NO = '123456789'


def event(event_id='evt_1', summary='周会', *, organizer='ou_A',
          start=NOW - 600, end=NOW + 3600, no=MEETING_NO, description='材料内容',
          status=None):
    item = {'event_id': event_id, 'summary': summary,
            'start_time': {'timestamp': str(start)}, 'end_time': {'timestamp': str(end)},
            'event_organizer': {'user_id': organizer},
            'description': description}
    if no:
        item['vchat'] = {'meeting_url': f'https://vc.feishu.cn/j/{no}'}
    if status:
        item['status'] = status
    return item


class FakeAuth:
    def __init__(self):
        self.calls = []

    def get_token(self, open_id, *, rejected_token=None):
        self.calls.append((open_id, rejected_token))
        return {'access_token': 'token-2' if rejected_token else 'token-1'}


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload, self.status = payload, status

    def read(self, _limit=None):
        return json.dumps(self.payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class Rejected:
    """Scripted provider rejection (non-zero Feishu API code)."""

    def __init__(self, code, status=403):
        self.code, self.status = code, status


class FakeHTTP:
    """Serves scripted responses by path; can reject a token once."""

    def __init__(self, pages, *, token_invalid_once=False):
        self.pages = pages
        self.token_invalid_once = token_invalid_once
        self.seen_tokens = []
        self.calls = []

    def __call__(self, req, timeout=None):
        path = req.selector.split('?')[0]
        self.calls.append(path)
        token = req.headers.get('Authorization', '')
        self.seen_tokens.append(token)
        if self.token_invalid_once:
            self.token_invalid_once = False
            raise error.HTTPError(req.full_url, 400, 'bad', {},
                                  io.BytesIO(json.dumps({'code': 99991661}).encode()))
        payload = self.pages.get(path)
        if payload is None and path.endswith('/attendees'):
            payload = {'items': [], 'has_more': False}  # default: no invitees
        if callable(payload):
            payload = payload(req)
        if isinstance(payload, Rejected):
            return FakeResponse({'code': payload.code, 'msg': 'rejected'},
                                status=payload.status)
        return FakeResponse({'code': 0, 'data': payload})


def pages(events):
    return {
        '/open-apis/authen/v1/user_info': {'user_id': None, 'open_id': 'ou_A',
                                           'union_id': 'un_A'},
        '/open-apis/calendar/v4/calendars': {'calendar_list': [
            {'calendar_id': 'cal_x', 'type': 'primary', 'role': 'owner'}]},
        '/open-apis/calendar/v4/calendars/cal_x/events': {'items': events},
    }


class ValidatorTests(unittest.TestCase):
    def make(self, events, *, token_invalid_once=False):
        auth = FakeAuth()
        http = FakeHTTP(pages(events), token_invalid_once=token_invalid_once)
        validator = FeishuCalendarValidator(auth, opener=http, clock=lambda: NOW)
        return validator, auth, http

    def test_happy_path_binds_and_carries_material(self):
        validator, _auth, _http = self.make([event(), event('evt_2', no='999888777')])
        binding = validator.validate('ou_A', {'no': MEETING_NO})
        self.assertEqual(binding['event_calendar_id'], 'cal_x')
        self.assertEqual(binding['event_id'], 'cal_x/evt_1')
        self.assertEqual(binding['title'], '周会')
        self.assertEqual(binding['description'], '材料内容')

    def test_meeting_not_found(self):
        validator, _a, _h = self.make([event(no='999888777')])
        with self.assertRaises(CalendarError) as ctx:
            validator.validate('ou_A', {'no': MEETING_NO})
        self.assertEqual(ctx.exception.code, 'meeting_not_found')

    def test_not_organizer_returns_flag(self):
        # v2: the admission decision moved to the API layer; the validator
        # reports organizer_ok instead of raising (STATE_MACHINES v2 §0).
        validator, _a, _h = self.make([event(organizer='ou_SOMEONE_ELSE')])
        binding = validator.validate('ou_A', {'no': MEETING_NO})
        self.assertFalse(binding['organizer_ok'])
        validator, _a, _h = self.make([event(organizer='ou_A')])
        self.assertTrue(validator.validate('ou_A', {'no': MEETING_NO})['organizer_ok'])

    def test_attendees_and_ambiguous(self):
        ev1 = event(organizer='ou_A')
        ev1['attendees'] = [{'open_id': 'ou_B'}, {'user_id': 'ou_C'}]
        validator, _a, _h = self.make([ev1])
        binding = validator.validate('ou_A', {'no': MEETING_NO})
        self.assertEqual(binding['attendees'], {'ou_B', 'ou_C'})
        validator2, _a2, _h2 = self.make([event(organizer='ou_A'),
                                          event(event_id='evt_2', organizer='ou_A')])
        with self.assertRaises(CalendarError) as ctx:
            validator2.validate('ou_A', {'no': MEETING_NO})
        self.assertEqual(ctx.exception.code, 'meeting_ambiguous')

    def test_outside_window_both_sides(self):
        for start, end in ((NOW - 7200, NOW - 3600), (NOW + 3600, NOW + 7200)):
            validator, _a, _h = self.make([event(start=start, end=end)])
            with self.assertRaises(CalendarError) as ctx:
                validator.validate('ou_A', {'no': MEETING_NO})
            self.assertEqual(ctx.exception.code, 'outside_window')

    def test_window_slack_30_minutes(self):
        validator, _a, _h = self.make([event(start=NOW + 25 * 60, end=NOW + 3600)])
        binding = validator.validate('ou_A', {'no': MEETING_NO})
        self.assertEqual(binding['event_id'], 'cal_x/evt_1')

    def test_cancelled_and_all_day_events_ignored(self):
        events = [event(status='cancelled'), event(start=None)]
        events[1].pop('start_time')
        validator, _a, _h = self.make(events)
        with self.assertRaises(CalendarError) as ctx:
            validator.validate('ou_A', {'no': MEETING_NO})
        self.assertEqual(ctx.exception.code, 'meeting_not_found')

    def test_token_rejection_triggers_one_silent_refresh(self):
        validator, auth, http = self.make([event()], token_invalid_once=True)
        binding = validator.validate('ou_A', {'no': MEETING_NO})
        self.assertEqual(binding['event_id'], 'cal_x/evt_1')
        self.assertEqual(http.seen_tokens[:2], ['Bearer token-1', 'Bearer token-2'])
        self.assertEqual(auth.calls[1][1], 'token-1')  # rejected token passed back

    def test_api_failure_is_auth_unavailable(self):
        validator, _a, http = self.make([event()])
        http.pages['/open-apis/calendar/v4/calendars'] = None  # unexpected shape
        with self.assertRaises(CalendarError) as ctx:
            validator.validate('ou_A', {'no': MEETING_NO})
        self.assertEqual(ctx.exception.code, 'auth_unavailable')

    def test_credentials_lost_mid_validation_maps_unavailable(self):
        class AuthNowLost(FakeAuth):
            def get_token(self, open_id, *, rejected_token=None):
                raise AuthorizationRequired('https://x/oauth')

        http = FakeHTTP(pages([event()]))
        validator = FeishuCalendarValidator(AuthNowLost(), opener=http, clock=lambda: NOW)
        with self.assertRaises(CalendarError) as ctx:
            validator.validate('ou_A', {'no': MEETING_NO})
        self.assertEqual(ctx.exception.code, 'auth_unavailable')


class ListMeetingsTests(unittest.TestCase):
    def make(self, events):
        auth = FakeAuth()
        http = FakeHTTP(pages(events))
        return FeishuCalendarValidator(auth, opener=http, clock=lambda: NOW)

    def test_lists_windowed_meetings_with_numbers(self):
        events = [event('evt_now', no='111111111', start=NOW - 600, end=NOW + 600),
                  event('evt_later', '下周会', no='222222222',
                        start=NOW + 2 * 86400, end=NOW + 2 * 86400 + 3600),
                  event('evt_far', no='333333333', start=NOW + 20 * 86400,
                        end=NOW + 20 * 86400 + 3600)]
        result = self.make(events).list_meetings('ou_A')
        numbers = [row['meeting_no'] for row in result['meetings']]
        self.assertEqual(numbers, ['111111111', '222222222'])  # far event filtered
        first = result['meetings'][0]
        self.assertTrue(first['in_progress'])
        self.assertTrue(first['is_organizer'])

    def test_events_without_vchat_have_no_number(self):
        events = [event('evt_no_vc', no=None)]
        result = self.make(events).list_meetings('ou_A')
        self.assertEqual(result['meetings'][0]['meeting_no'], None)

    def test_non_organizer_flag_and_limit(self):
        events = [event(f'evt_{i}', no=f'10000000{i}', start=NOW + i * 7200,
                        end=NOW + i * 7200 + 600, organizer='ou_OTHER')
                  for i in range(12)]
        result = self.make(events).list_meetings('ou_A')
        self.assertLessEqual(len(result['meetings']), 10)
        self.assertFalse(result['meetings'][0]['is_organizer'])


class RecurringSeriesTests(unittest.TestCase):
    """2026-11-18 bug (小A daily meeting): the events list API returns series
    masters (<uid>_0, first-occurrence timestamps) plus exceptions, not
    expanded occurrences. A daily series with past occurrences must still
    bind/list today's occurrence and split series must not collide."""

    TZ = ZoneInfo('Asia/Shanghai')

    def local_ts(self, *parts):
        return int(datetime(*parts, tzinfo=self.TZ).timestamp())

    def setUp(self):
        self.now = self.local_ts(2033, 5, 18, 10, 49)  # a Wednesday
        self.today = self.local_ts(2033, 5, 18, 11, 0)

    def make(self, events):
        auth = FakeAuth()
        http = FakeHTTP(pages(events))
        return FeishuCalendarValidator(auth, opener=http, clock=lambda: self.now)

    def master(self, event_id, start, recurrence='FREQ=DAILY;INTERVAL=1', **extra):
        item = event(event_id, extra.pop('summary', '每日站会'), organizer='ou_A',
                     start=start, end=extra.pop('end', start + 1800),
                     no=extra.pop('no', MEETING_NO))
        item['recurrence'] = recurrence
        item.update(extra)
        return item

    def test_daily_series_binds_and_lists_todays_occurrence(self):
        series_start = self.local_ts(2033, 5, 11, 11, 0)  # first occurrence
        validator = self.make([self.master('evt_d_0', series_start)])
        binding = validator.validate('ou_A', {'no': MEETING_NO})
        self.assertEqual(binding['event_id'], f'cal_x/evt_d_{self.today}')
        self.assertEqual((binding['start'], binding['end']),
                         (self.today, self.today + 1800))
        self.assertEqual(binding['title'], '每日站会')
        self.assertTrue(binding['organizer_ok'])
        rows = validator.list_meetings('ou_A')['meetings']
        # master expanded: today through the days=7 horizon, no past rows
        self.assertEqual([row['start_ts'] for row in rows],
                         [self.today + 86400 * i for i in range(7)])
        self.assertTrue(all(row['meeting_no'] == MEETING_NO for row in rows))

    def test_ended_series_reports_meeting_not_found(self):
        series_start = self.local_ts(2033, 5, 11, 11, 0)
        validator = self.make([self.master(
            'evt_d_0', series_start, recurrence='FREQ=DAILY;UNTIL=20330515T030000Z')])
        with self.assertRaises(CalendarError) as ctx:
            validator.validate('ou_A', {'no': MEETING_NO})
        self.assertEqual(ctx.exception.code, 'meeting_not_found')

    def test_count_exhausted_series_reports_meeting_not_found(self):
        series_start = self.local_ts(2033, 5, 11, 11, 0)
        validator = self.make([self.master('evt_d_0', series_start,
                                           recurrence='FREQ=DAILY;COUNT=3')])
        with self.assertRaises(CalendarError) as ctx:
            validator.validate('ou_A', {'no': MEETING_NO})
        self.assertEqual(ctx.exception.code, 'meeting_not_found')

    def test_split_series_resolves_the_live_one(self):
        # "此日程及后续" edit: two masters share one meeting_no; only the new
        # series still has occurrences now.
        old = self.master('evt_o_0', self.local_ts(2033, 5, 3, 11, 0),
                          recurrence='FREQ=DAILY;UNTIL=20330515T235959Z')
        new = self.master('evt_n_0', self.local_ts(2033, 5, 15, 11, 0))
        validator = self.make([old, new])
        binding = validator.validate('ou_A', {'no': MEETING_NO})
        self.assertEqual(binding['event_id'], f'cal_x/evt_n_{self.today}')

    def test_two_live_series_same_no_stay_ambiguous(self):
        first = self.master('evt_a_0', self.local_ts(2033, 5, 11, 11, 0))
        second = self.master('evt_b_0', self.local_ts(2033, 5, 15, 11, 0))
        validator = self.make([first, second])
        with self.assertRaises(CalendarError) as ctx:
            validator.validate('ou_A', {'no': MEETING_NO})
        self.assertEqual(ctx.exception.code, 'meeting_ambiguous')

    def exception(self, master_id, original_start, start, **extra):
        """Edited instance: docs shape = recurrence empty + is_exception."""
        uid = master_id[:-2] if master_id.endswith('_0') else master_id
        item = self.master(f'{uid}_{original_start}', start, **extra)
        item['recurrence'] = ''
        item['is_exception'] = True
        item['recurring_event_id'] = master_id
        return item

    def test_exception_overrides_its_slot(self):
        series_start = self.local_ts(2033, 5, 11, 11, 0)
        moved = self.exception('evt_d_0', self.today, self.today - 19 * 60,
                               end=self.today + 3600 - 19 * 60)
        validator = self.make([self.master('evt_d_0', series_start), moved])
        binding = validator.validate('ou_A', {'no': MEETING_NO})
        self.assertEqual(binding['event_id'], f'cal_x/evt_d_{self.today}')
        self.assertEqual(binding['start'], self.today - 19 * 60)
        rows = [row for row in validator.list_meetings('ou_A')['meetings']
                if row['start_ts'] == self.today]  # original slot not listed
        self.assertEqual(rows, [])

    def test_cancelled_exception_deletes_occurrence(self):
        series_start = self.local_ts(2033, 5, 11, 11, 0)
        deleted = self.exception('evt_d_0', self.today, self.today)
        deleted['status'] = 'cancelled'
        validator = self.make([self.master('evt_d_0', series_start), deleted])
        with self.assertRaises(CalendarError) as ctx:
            validator.validate('ou_A', {'no': MEETING_NO})
        self.assertEqual(ctx.exception.code, 'outside_window')
        starts = [row['start_ts'] for row in validator.list_meetings('ou_A')['meetings']]
        self.assertNotIn(self.today, starts)

    def test_weekly_byday_binds_midweek_occurrence(self):
        validator = self.make([self.master(
            'evt_w_0', self.local_ts(2033, 5, 7, 11, 0),  # a Friday
            recurrence='FREQ=WEEKLY;BYDAY=MO,WE')])
        binding = validator.validate('ou_A', {'no': MEETING_NO})
        self.assertEqual(binding['event_id'], f'cal_x/evt_w_{self.today}')

    def test_weekly_series_between_occurrences_is_outside_window(self):
        # next occurrence tomorrow, none covers now
        validator = self.make([self.master(
            'evt_w_0', self.local_ts(2033, 5, 17, 11, 0),  # a Tuesday
            recurrence='FREQ=WEEKLY;BYDAY=TU')])
        with self.assertRaises(CalendarError) as ctx:
            validator.validate('ou_A', {'no': MEETING_NO})
        self.assertEqual(ctx.exception.code, 'outside_window')

    def test_monthly_series_binds_same_day_occurrence(self):
        validator = self.make([self.master(
            'evt_m_0', self.local_ts(2033, 3, 18, 11, 0),
            recurrence='FREQ=MONTHLY;INTERVAL=1')])
        binding = validator.validate('ou_A', {'no': MEETING_NO})
        self.assertEqual(binding['start'], self.today)

    def test_unsupported_rule_falls_back_to_first_occurrence(self):
        # FREQ=MONTHLY;BYDAY=2TU is not expandable: keep the pre-fix behavior
        # (bind the master around its first occurrence) instead of failing.
        validator = self.make([self.master(
            'evt_m_0', self.today, recurrence='FREQ=MONTHLY;BYDAY=2TU')])
        binding = validator.validate('ou_A', {'no': MEETING_NO})
        self.assertEqual(binding['start'], self.today)
        self.assertEqual(binding['event_id'], f'cal_x/evt_m_{self.today}')


class AttendeeFetchTests(unittest.TestCase):
    """2026-11-18 bug (supervisor not_invited): the events list API omits
    attendees[]; the 小C admission path must fetch the per-occurrence invite
    list from the dedicated attendees endpoint."""

    def make(self, events, attendees=None):
        auth = FakeAuth()
        pages_ = pages(events)
        if attendees is not None:
            pages_['/open-apis/calendar/v4/calendars/cal_x/events/evt_1/attendees'] = attendees
        http = FakeHTTP(pages_)
        validator = FeishuCalendarValidator(auth, opener=http, clock=lambda: NOW)
        return validator, http

    def test_organizer_path_does_not_fetch_attendees(self):
        validator, http = self.make([event()])
        binding = validator.validate('ou_A', {'no': MEETING_NO})
        self.assertTrue(binding['organizer_ok'])
        self.assertFalse(any(path.endswith('/attendees') for path in http.calls))

    def test_supervisor_membership_fetched_from_attendees_endpoint(self):
        # organized by someone else, no attendees[] in the list item
        pages_payload = {'items': [
            {'type': 'user', 'user_id': 'ou_A', 'rsvp_status': 'accept'},
            {'type': 'user', 'user_id': 'ou_OTHER', 'rsvp_status': 'needs_action'},
            {'type': 'user', 'user_id': 'ou_GONE', 'rsvp_status': 'removed'},
            {'type': 'chat', 'attendee_id': 'chat_x', 'chat_id': 'oc_x'},
            {'type': 'chat', 'attendee_id': 'chat_gone', 'rsvp_status': 'removed'},
            {'type': 'resource', 'room_id': 'omp_x'},
        ], 'has_more': False}
        validator, http = self.make([event(organizer='ou_ORGANIZER')], pages_payload)
        binding = validator.validate('ou_A', {'no': MEETING_NO})
        self.assertFalse(binding['organizer_ok'])
        self.assertEqual(binding['attendees'], {'ou_A', 'ou_OTHER'})
        self.assertEqual(binding['chat_attendee_ids'], ['chat_x'])
        self.assertIn('/open-apis/calendar/v4/calendars/cal_x/events/evt_1/attendees',
                      http.calls)

    def test_absent_invitee_yields_empty_membership(self):
        pages_payload = {'items': [
            {'type': 'user', 'user_id': 'ou_OTHER'}], 'has_more': False}
        validator, _http = self.make([event(organizer='ou_ORGANIZER')], pages_payload)
        binding = validator.validate('ou_A', {'no': MEETING_NO})
        self.assertNotIn('ou_A', binding['attendees'])

    def test_attendee_pagination_follows_page_token(self):
        def serve(req):
            if 'page_token' not in req.selector:
                return {'items': [{'type': 'user', 'user_id': 'ou_OTHER'}],
                        'has_more': True, 'page_token': 'page-2'}
            return {'items': [{'type': 'user', 'user_id': 'ou_A'}], 'has_more': False}
        validator, http = self.make([event(organizer='ou_ORGANIZER')], serve)
        binding = validator.validate('ou_A', {'no': MEETING_NO})
        self.assertIn('ou_A', binding['attendees'])
        attendees_calls = [p for p in http.calls if p.endswith('/attendees')]
        self.assertEqual(attendees_calls, [attendees_calls[0]] * 2)

    def test_series_occurrence_fetches_instance_attendees(self):
        t = RecurringSeriesTests()
        t.setUp()
        series = t.master('evt_d_0', t.local_ts(2033, 5, 11, 11, 0))
        series['event_organizer'] = {'user_id': 'ou_ORGANIZER'}  # force C path
        auth = FakeAuth()
        pages_ = pages([series])
        instance_path = (f'/open-apis/calendar/v4/calendars/cal_x/events/'
                         f'evt_d_{t.today}/attendees')
        pages_[instance_path] = {'items': [
            {'type': 'user', 'user_id': 'ou_A'}], 'has_more': False}
        http = FakeHTTP(pages_)
        validator = FeishuCalendarValidator(auth, opener=http, clock=lambda: t.now)
        binding = validator.validate('ou_A', {'no': MEETING_NO})
        self.assertEqual(binding['event_id'], f'cal_x/evt_d_{t.today}')
        self.assertEqual(binding['attendees'], {'ou_A'})
        self.assertIn(instance_path, http.calls)


ATTENDEES_PATH = '/open-apis/calendar/v4/calendars/cal_x/events/evt_1/attendees'


def chat_path(attendee_id):
    return f'{ATTENDEES_PATH}/{attendee_id}/chat_members'


class ChatExpansionTests(unittest.TestCase):
    """v2.6 (2026-11-19 bug): a supervisor whose only invite proof is
    membership in an invited chat group must hit via the dedicated
    chat_members endpoint instead of being rejected not_invited."""

    def make(self, attendee_items, chat_pages=None):
        auth = FakeAuth()
        pages_ = pages([event(organizer='ou_ORGANIZER')])
        pages_[ATTENDEES_PATH] = {'items': attendee_items, 'has_more': False}
        for path, payload in (chat_pages or {}).items():
            pages_[path] = payload
        http = FakeHTTP(pages_)
        validator = FeishuCalendarValidator(auth, opener=http, clock=lambda: NOW)
        binding = validator.validate('ou_A', {'no': MEETING_NO})
        self.assertFalse(binding['organizer_ok'])
        return validator, binding, http

    def test_group_member_hits_via_chat_members(self):
        attendee_items = [
            {'type': 'user', 'user_id': 'ou_ORGANIZER'},
            {'type': 'chat', 'attendee_id': 'chat_x', 'chat_id': 'oc_x'}]
        validator, binding, http = self.make(
            attendee_items, {chat_path('chat_x'): {
                'items': [{'open_id': 'ou_OTHER'}, {'union_id': 'un_A'}],
                'has_more': False}})
        self.assertEqual(binding['attendees'], {'ou_ORGANIZER'})
        self.assertTrue(validator.chat_member_hit('ou_A', binding,
                                                  {'ou_A', 'un_A', 'ou_A_user'}))
        self.assertIn(chat_path('chat_x'), http.calls)

    def test_no_hit_returns_false(self):
        validator, binding, _http = self.make(
            [{'type': 'chat', 'attendee_id': 'chat_x'}],
            {chat_path('chat_x'): {
                'items': [{'open_id': 'ou_OTHER'}], 'has_more': False}})
        self.assertFalse(validator.chat_member_hit('ou_A', binding, {'ou_A'}))

    def test_removed_chat_member_not_counted(self):
        validator, binding, _http = self.make(
            [{'type': 'chat', 'attendee_id': 'chat_x'}],
            {chat_path('chat_x'): {
                'items': [{'open_id': 'ou_A', 'rsvp_status': 'removed'}],
                'has_more': False}})
        self.assertFalse(validator.chat_member_hit('ou_A', binding, {'ou_A'}))

    def test_pagination_follows_until_hit(self):
        def serve(req):
            if 'page_token' not in req.selector:
                return {'items': [{'open_id': 'ou_OTHER'}], 'has_more': True,
                        'page_token': 'p1'}
            return {'items': [{'user_id': 'ou_A'}], 'has_more': False}
        validator, binding, http = self.make(
            [{'type': 'chat', 'attendee_id': 'chat_x'}],
            {chat_path('chat_x'): serve})
        self.assertTrue(validator.chat_member_hit('ou_A', binding, {'ou_A'}))
        self.assertEqual(http.calls.count(chat_path('chat_x')), 2)

    def test_provider_rejection_skips_chat_and_tries_next(self):
        # caller outside the chat / attendee list hidden: deterministic
        # "no proof from this chat", never a fail-closed denial
        validator, binding, http = self.make(
            [{'type': 'chat', 'attendee_id': 'chat_1'},
             {'type': 'chat', 'attendee_id': 'chat_2'}],
            {chat_path('chat_1'): Rejected(194001),
             chat_path('chat_2'): {'items': [{'open_id': 'ou_A'}],
                                   'has_more': False}})
        self.assertTrue(validator.chat_member_hit('ou_A', binding, {'ou_A'}))
        self.assertIn(chat_path('chat_1'), http.calls)

    def test_transport_failure_fails_closed(self):
        def boom(_req):
            raise OSError('network down')
        validator, binding, _http = self.make(
            [{'type': 'chat', 'attendee_id': 'chat_x'}],
            {chat_path('chat_x'): boom})
        with self.assertRaises(CalendarError) as ctx:
            validator.chat_member_hit('ou_A', binding, {'ou_A'})
        self.assertEqual(ctx.exception.code, 'auth_unavailable')

    def test_truncation_fails_closed(self):
        counter = {'n': 0}

        def serve(_req):
            counter['n'] += 1
            return {'items': [{'open_id': 'ou_OTHER'}], 'has_more': True,
                    'page_token': f'p{counter["n"]}'}
        validator, binding, _http = self.make(
            [{'type': 'chat', 'attendee_id': 'chat_x'}],
            {chat_path('chat_x'): serve})
        with self.assertRaises(CalendarError) as ctx:
            validator.chat_member_hit('ou_A', binding, {'ou_A'})
        self.assertEqual(ctx.exception.code, 'auth_unavailable')
        self.assertEqual(counter['n'], 20)

    def test_series_instance_uses_instance_id_for_members(self):
        t = RecurringSeriesTests()
        t.setUp()
        series = t.master('evt_d_0', t.local_ts(2033, 5, 11, 11, 0))
        series['event_organizer'] = {'user_id': 'ou_ORGANIZER'}
        auth = FakeAuth()
        pages_ = pages([series])
        instance_path = (f'/open-apis/calendar/v4/calendars/cal_x/events/'
                         f'evt_d_{t.today}/attendees')
        pages_[instance_path] = {'items': [
            {'type': 'chat', 'attendee_id': 'chat_x'}], 'has_more': False}
        pages_[f'{instance_path}/chat_x/chat_members'] = {
            'items': [{'open_id': 'ou_A'}], 'has_more': False}
        http = FakeHTTP(pages_)
        validator = FeishuCalendarValidator(auth, opener=http,
                                            clock=lambda: t.now)
        binding = validator.validate('ou_A', {'no': MEETING_NO})
        self.assertTrue(validator.chat_member_hit('ou_A', binding, {'ou_A'}))
        self.assertIn(f'{instance_path}/chat_x/chat_members', http.calls)


if __name__ == '__main__':
    unittest.main()
