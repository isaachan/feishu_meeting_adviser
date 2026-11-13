"""Offline contract checks for the standalone polling probe."""

import io
import json
from contextlib import redirect_stdout
from unittest import TestCase, main
from unittest.mock import patch

import listener


class _FakeResponse:
    def __init__(self, body):
        self._body = json.dumps(body).encode()

    def read(self, *_args):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class ListenerTests(TestCase):
    def test_api_get_unwraps_data_envelope_once(self):
        # VC endpoints wrap payloads in data; the token endpoint does not. json_request
        # must return the full envelope so each caller unwraps exactly one level.
        envelope = {'code': 0, 'data': {'meetings': []}}
        with patch.object(listener.request, 'urlopen', return_value=_FakeResponse(envelope)):
            self.assertEqual(listener.api_get(listener.ACTIVE, 'user-token'), {'meetings': []})

    def test_token_exchange_response_has_top_level_fields(self):
        # The authen v2 token endpoint returns access_token/scope at the top level.
        result = {'code': 0, 'access_token': 'u-token', 'scope': listener.SCOPE,
                  'refresh_token': 'ur-token', 'expires_in': 6900, 'token_type': 'Bearer'}
        self.assertEqual(listener.parse_token_response(result), 'u-token')
        # Missing scope only warns; the VC API gives the definitive error.
        self.assertEqual(listener.parse_token_response({**result, 'scope': 'other:scope'}), 'u-token')
        for bad in ({'data': result, 'code': 0}, {**result, 'access_token': ''}):
            with self.assertRaises(listener.ListenerError):
                listener.parse_token_response(bad)

    def test_choose_meeting_requires_unique_active_user_meeting(self):
        meetings = {'meetings': [
            {'meeting_no': '012345678', 'meeting_id': 'long-1'},
            {'meeting_no': '123456789', 'meeting_id': 'long-2'},
        ]}
        with patch.object(listener, 'api_get', return_value=meetings) as get:
            self.assertEqual(listener.choose_meeting('user-token', '012345678'), 'long-1')
            self.assertEqual(get.call_args.args, (listener.ACTIVE, 'user-token'))
            with self.assertRaises(listener.ListenerError):
                listener.choose_meeting('user-token')

    def test_meeting_gate_120002_is_retried_not_fatal(self):
        output = io.StringIO()
        gated = listener.ListenerError('Feishu HTTP 403, API code=120002', code=120002)
        with patch.object(listener, 'api_get', side_effect=gated), redirect_stdout(output):
            listener.listen('user-token', 'long-1', once=True)  # must not raise
        self.assertEqual(output.getvalue(), '')

    def test_transient_failures_are_retried_and_persistent_errors_exit(self):
        output = io.StringIO()
        transient = listener.ListenerError('Feishu network request failed', transient=True)
        with patch.object(listener, 'api_get', side_effect=transient), redirect_stdout(output):
            listener.listen('user-token', 'long-1', once=True)  # must not raise
        self.assertEqual(output.getvalue(), '')
        persistent = listener.ListenerError('Feishu HTTP 403, API code=121005', code=121005)
        with patch.object(listener, 'api_get', side_effect=persistent), redirect_stdout(output):
            with self.assertRaises(listener.ListenerError):
                listener.listen('user-token', 'long-1', once=True)

    def test_count_events_labels_activity_subtypes(self):
        page = {'events': [
            {'event_type': 'meeting.activity', 'payload': {'activity_event_type': 'transcript_received',
                                                           'transcript_received_items': [{'text': 'hi'}]}},
            {'event_type': 'meeting.activity', 'payload': {'activity_event_type': 'participant_joined'}},
            {'event_type': 'meeting.activity', 'payload': {'activity_event_type': 'chat_received'}},
            {'event_type': 'something.else', 'payload': {}},
        ]}
        self.assertEqual(listener.count_events(page),
                         {'transcript': 1, 'chat': 1, 'join': 1, 'left': 0, 'other': 1})

    def test_poll_paginated_events_prints_once_and_retains_query_across_pages(self):
        event = {'event_id': 'evt-1', 'event_type': 'meeting.activity', 'payload': {
            'meeting': {'id': 'long-1'}, 'activity_event_type': 'transcript_received',
            'transcript_received_items': [{'sentence_id': 's1', 'text': '你好',
                                           'speaker': {'user_name': '甲'}}]}}
        page1 = {'events': [event], 'has_more': True, 'page_token': 'next'}
        page2 = {'events': [event, {'event_type': 'transcript_received', 'event_id': 'evt-2',
                                   'payload': {'text': '收到', 'speaker': {'user_name': '乙'}}}],
                 'has_more': False}
        output = io.StringIO()
        queries = []

        def get_page(_url, _token, query):
            queries.append(dict(query))
            return [page1, page2][len(queries) - 1]

        with patch.object(listener, 'api_get', side_effect=get_page), redirect_stdout(output):
            listener.listen('user-token', 'long-1', once=True)
        self.assertEqual(output.getvalue().splitlines(), ['甲: 你好', '乙: 收到'])
        first, second = queries
        self.assertEqual(first['meeting_id'], 'long-1')
        self.assertEqual(first['start_time'], second['start_time'])
        self.assertEqual(first['end_time'], second['end_time'])
        self.assertNotIn('page_token', first)
        self.assertEqual(second['page_token'], 'next')


if __name__ == '__main__':
    main()
