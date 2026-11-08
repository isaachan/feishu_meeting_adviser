"""Offline contract checks for the standalone polling probe."""

import io
from contextlib import redirect_stdout
from unittest import TestCase, main
from unittest.mock import patch

import listener


class ListenerTests(TestCase):
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
