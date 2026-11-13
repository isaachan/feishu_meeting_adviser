import json
from pathlib import Path
import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit

from local_meeting_recorder.feishu_calendar import MeetingCalendar
from local_meeting_recorder.oauth import (
    SCOPES, FeishuAppCredentials, OAuthManager,
)

FIXED_NOW = 2_000_000
WINDOW = 1800


def _reply(body, status=200):
    class Response:
        def __init__(self, payload):
            self._payload = json.dumps(payload).encode()
            self.status = status

        def read(self, limit=-1):
            return self._payload

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    return Response(body)


def token_result(access='private-access'):
    return {'code': 0, 'access_token': access, 'refresh_token': access + '-r',
            'expires_in': 7200, 'refresh_token_expires_in': 604800,
            'scope': ' '.join(SCOPES), 'token_type': 'Bearer'}


class RefreshingTransport:
    """OAuth transport whose access token rotates on refresh."""

    def __init__(self):
        self.calls = []

    def exchange(self, code, redirect_uri, verifier):
        return token_result('private-access')

    def subject(self, token):
        return 'hash'

    def refresh(self, refresh_token, *, timeout=None):
        self.calls.append(refresh_token)
        return token_result('next-private-access')


class StubOpener:
    """Routes GETs by (path, token) to scripted JSON replies."""

    def __init__(self):
        self.routes = {}
        self.requests = []

    def add(self, path_prefix, token, payload, *, status=200, code=None):
        if code is not None:
            payload = {'code': code, 'msg': 'rejected'}
        self.routes[(path_prefix, token)] = _reply(payload, status)

    def __call__(self, req, timeout=None):
        parsed = urlsplit(req.full_url)
        path = parsed.path
        token = req.get_header('Authorization', '').removeprefix('Bearer ')
        self.requests.append((path, token, parse_qs(parsed.query)))
        routes = sorted(self.routes.items(),
                        key=lambda item: len(item[0][0]), reverse=True)
        for (prefix, route_token), reply in routes:
            if path.startswith(prefix) and route_token == token:
                return reply
        return _reply({'code': 1900001, 'msg': 'no route'}, status=404)


class MeetingCalendarTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.transport = RefreshingTransport()
        self.manager = OAuthManager(
            FeishuAppCredentials('app', 'secret'), Path(self.temp.name),
            transport=self.transport, clock=lambda: 1000.0)
        self.opener = StubOpener()
        self.calendar = MeetingCalendar(
            self.manager, opener=self.opener, clock=lambda: FIXED_NOW,
            base_url='https://feishu.example')
        self.manager.authorize('code-1',
                               'http://127.0.0.1:8765/oauth/callback', 'v')

    def route_defaults(self, *, events_token='private-access'):
        self.opener.add('/open-apis/authen/v1/user_info', events_token,
                        {'code': 0, 'data': {'open_id': 'ou_1',
                                             'union_id': 'on_1'}})
        self.opener.add('/open-apis/calendar/v4/calendars', events_token,
                        {'code': 0, 'data': {'calendar_list': [
                            {'calendar_id': 'cal_shared', 'type': 'shared'},
                            {'calendar_id': 'cal_primary', 'type': 'primary'},
                        ]}})

    def events_reply(self, items):
        return {'code': 0, 'data': {'items': items}}

    def test_my_meetings_annotates_organizer_and_filters_window(self):
        self.route_defaults()
        self.opener.add('/open-apis/calendar/v4/calendars/cal_primary/events',
                        'private-access', self.events_reply([
                            {'event_id': 'evt_own', 'summary': '我的周会',
                             'status': 'confirmed',
                             'event_organizer': {'display_name': '我',
                                                 'user_id': 'ou_1'},
                             'start_time': {'timestamp': str(FIXED_NOW - 600)},
                             'end_time': {'timestamp': str(FIXED_NOW + 600)},
                             'vchat': {'meeting_url': 'https://vc.feishu.cn/j/123456789'}},
                            {'event_id': 'cal_other/evt_other', 'summary': '别人的会',
                             'status': 'confirmed',
                             'event_organizer': {'user_id': 'ou_9'},
                             'start_time': {'timestamp': str(FIXED_NOW - 60)},
                             'end_time': {'timestamp': str(FIXED_NOW + 60)}},
                            {'event_id': 'evt_cancelled', 'summary': '已取消的会',
                             'status': 'cancelled',
                             'event_organizer': {'user_id': 'ou_1'},
                             'start_time': {'timestamp': str(FIXED_NOW - 60)},
                             'end_time': {'timestamp': str(FIXED_NOW + 60)}},
                            {'event_id': 'evt_all_day', 'summary': '全天事件',
                             'start_time': {'date': '2026-11-10'},
                             'end_time': {'date': '2026-11-10'}},
                            {'event_id': 'evt_later', 'summary': '三小时后',
                             'start_time': {'timestamp': str(FIXED_NOW + 10800)},
                             'end_time': {'timestamp': str(FIXED_NOW + 14400)}},
                        ]))
        report = self.calendar.my_meetings(window_seconds=WINDOW)
        self.assertEqual(report['calendar_id'], 'cal_primary')
        self.assertEqual([meeting['event_id'] for meeting in report['meetings']],
                         ['cal_primary/evt_own', 'cal_other/evt_other'])
        own, other = report['meetings']
        self.assertTrue(own['is_organizer'])
        self.assertTrue(own['online_meeting'])
        self.assertFalse(other['is_organizer'])
        self.assertFalse(other['online_meeting'])

    def test_token_rejection_triggers_refresh_and_retry(self):
        self.route_defaults()
        events_path = '/open-apis/calendar/v4/calendars/cal_primary/events'
        self.opener.add(events_path, 'private-access', None, code=99991668)
        self.opener.add(events_path, 'next-private-access',
                        self.events_reply([
                            {'event_id': 'evt_own', 'summary': '重试后的会',
                             'status': 'confirmed',
                             'event_organizer': {'user_id': 'ou_1'},
                             'start_time': {'timestamp': str(FIXED_NOW - 10)},
                             'end_time': {'timestamp': str(FIXED_NOW + 10)}},
                        ]))
        report = self.calendar.my_meetings(window_seconds=WINDOW)
        self.assertEqual(len(report['meetings']), 1)
        self.assertEqual(self.transport.calls, ['private-access-r'])
        event_requests = [entry for entry in self.opener.requests
                          if 'start_time' in entry[2]]
        self.assertEqual([entry[1] for entry in event_requests],
                         ['private-access', 'next-private-access'])

    def test_primary_falls_back_to_owned_calendar(self):
        self.opener.add('/open-apis/authen/v1/user_info', 'private-access',
                        {'code': 0, 'data': {'user_id': 'u_1'}})
        self.opener.add('/open-apis/calendar/v4/calendars', 'private-access',
                        {'code': 0, 'data': {'calendar_list': [
                            {'calendar_id': 'cal_owned', 'role': 'owner'},
                        ]}})
        self.opener.add('/open-apis/calendar/v4/calendars/cal_owned/events',
                        'private-access', self.events_reply([]))
        report = self.calendar.my_meetings(window_seconds=WINDOW)
        self.assertEqual(report['calendar_id'], 'cal_owned')
        self.assertEqual(report['meetings'], [])

    def test_no_readable_calendar_returns_empty(self):
        self.opener.add('/open-apis/authen/v1/user_info', 'private-access',
                        {'code': 0, 'data': {'user_id': 'u_1'}})
        self.opener.add('/open-apis/calendar/v4/calendars', 'private-access',
                        {'code': 0, 'data': {'calendar_list': []}})
        report = self.calendar.my_meetings(window_seconds=WINDOW)
        self.assertIsNone(report['calendar_id'])
        self.assertEqual(report['meetings'], [])


if __name__ == '__main__':
    unittest.main()
