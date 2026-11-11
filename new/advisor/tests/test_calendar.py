"""Real calendar validator unit tests: injected HTTP responses, all four S1
rejection paths, meeting_url matching, silent token refresh, material pickup."""
import io
import json
import time
import unittest
from urllib import error

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
        payload = self.pages[path]
        if callable(payload):
            payload = payload(req)
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

    def test_not_organizer(self):
        validator, _a, _h = self.make([event(organizer='ou_SOMEONE_ELSE')])
        with self.assertRaises(CalendarError) as ctx:
            validator.validate('ou_A', {'no': MEETING_NO})
        self.assertEqual(ctx.exception.code, 'not_organizer')

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


if __name__ == '__main__':
    unittest.main()
