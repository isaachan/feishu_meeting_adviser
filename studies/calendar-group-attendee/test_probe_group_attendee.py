"""Offline tests for the pure helpers of probe_group_attendee.py."""

import unittest
from datetime import datetime

from probe_group_attendee import (
    TZ, chat_attendee_ids, any_id_hit, meeting_no, resolve_instance)

NO = '123456789'


def ts(year, month, day, hour, minute=0):
    return int(datetime(year, month, day, hour, minute, tzinfo=TZ).timestamp())


def event(event_id, start, end, recurrence=None, **extra):
    row = {'event_id': event_id,
           'start_time': {'timestamp': start},
           'end_time': {'timestamp': end},
           'vchat': {'meeting_url': f'https://vc.feishu.cn/j/{NO}'}}
    if recurrence:
        row['recurrence'] = recurrence
    row.update(extra)
    return row


class MeetingNoTests(unittest.TestCase):
    def test_extracts_nine_digit_number(self):
        self.assertEqual(meeting_no(
            {'vchat': {'meeting_url': 'https://vc.feishu.cn/j/987654321?x=1'}}),
            '987654321')

    def test_none_without_vchat_or_url(self):
        self.assertIsNone(meeting_no({}))
        self.assertIsNone(meeting_no({'vchat': {'meeting_url': None}}))
        self.assertIsNone(meeting_no({'vchat': {'meeting_url': 'https://x/'}}))


class ChatAttendeeTests(unittest.TestCase):
    def test_picks_chat_type_by_type_or_prefix(self):
        items = [{'type': 'user', 'attendee_id': 'user_1', 'user_id': 'ou_A'},
                 {'type': 'chat', 'attendee_id': 'chat_1', 'chat_id': 'oc_x'},
                 {'attendee_id': 'chat_2'},   # type missing: prefix fallback
                 {'type': 'resource', 'attendee_id': 'resource_1'},
                 {'type': 'chat'},            # no attendee_id: skipped
                 'junk']
        self.assertEqual(chat_attendee_ids(items), ['chat_1', 'chat_2'])

    def test_any_id_hit_scans_all_id_keys(self):
        self.assertTrue(any_id_hit([{'open_id': 'ou_A'}], {'ou_A'}))
        self.assertTrue(any_id_hit([{'user_id': 'ou_A'}], {'ou_A'}))
        self.assertTrue(any_id_hit([{'union_id': 'un_A'}], {'ou_A', 'un_A'}))
        self.assertFalse(any_id_hit([{'display_name': 'A'}], {'ou_A'}))
        self.assertFalse(any_id_hit(None, {'ou_A'}))


class ResolveInstanceTests(unittest.TestCase):
    def test_direct_event_binds_own_id(self):
        now = ts(2033, 5, 19, 11, 5)
        rows = resolve_instance([event('evt_1', now - 600, now + 600)], NO, now)
        self.assertEqual([row[1] for row in rows], ['evt_1'])

    def test_daily_master_binds_today_instance_id(self):
        start0 = ts(2033, 5, 10, 11, 0)
        now = ts(2033, 5, 19, 11, 5)
        expected = ts(2033, 5, 19, 11, 0)
        rows = resolve_instance(
            [event('evt_d_0', start0, start0 + 3600, recurrence='FREQ=DAILY')],
            NO, now)
        self.assertEqual([row[1] for row in rows], [f'evt_d_{expected}'])
        self.assertEqual(rows[0][2], expected)

    def test_weekly_master_expands_byday(self):
        start0 = ts(2033, 5, 11, 11, 0)  # a Wednesday
        now = ts(2033, 5, 18, 11, 5)     # the next Wednesday
        expected = ts(2033, 5, 18, 11, 0)
        rows = resolve_instance(
            [event('evt_w_0', start0, start0 + 3600,
                   recurrence='FREQ=WEEKLY;BYDAY=WE')], NO, now)
        self.assertEqual([row[1] for row in rows], [f'evt_w_{expected}'])

    def test_unsupported_rule_falls_back_to_master_id(self):
        start0 = ts(2033, 5, 19, 11, 0)
        now = ts(2033, 5, 19, 11, 5)
        rows = resolve_instance(
            [event('evt_m_0', start0, start0 + 3600,
                   recurrence='FREQ=MONTHLY;BYDAY=2TU')], NO, now)
        self.assertEqual([row[1] for row in rows], ['evt_m_0'])

    def test_out_of_window_occurrence_is_not_returned(self):
        now = ts(2033, 5, 19, 9, 0)      # 2h before today's 11:00 occurrence
        rows = resolve_instance(
            [event('evt_d_0', ts(2033, 5, 10, 11, 0), ts(2033, 5, 10, 12, 0),
             recurrence='FREQ=DAILY')], NO, now)
        self.assertEqual(rows, [])

    def test_cancelled_and_foreign_events_are_skipped(self):
        now = ts(2033, 5, 19, 11, 5)
        rows = resolve_instance(
            [event('evt_x', now - 600, now + 600, status='cancelled'),
             event('evt_y', now - 600, now + 600,
                   vchat={'meeting_url': 'https://vc.feishu.cn/j/999999999'})],
            NO, now)
        self.assertEqual(rows, [])

    def test_removed_attendee_filter_is_caller_side(self):
        # resolve_instance itself only picks occurrences; the rsvp filter is
        # applied by the caller on attendee items (mirrors advisor). Guard
        # the helper contract used by the verdict computation.
        self.assertEqual(chat_attendee_ids(
            [{'type': 'chat', 'attendee_id': 'chat_1', 'rsvp_status': 'removed'}]),
            ['chat_1'])


if __name__ == '__main__':
    unittest.main()
