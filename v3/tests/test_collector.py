import json
import tempfile
import unittest
from pathlib import Path

from feishu_spectator.collector import (ListenSettings, ListenerStopped, PageFormatError,
                                        Spectator, parse_page)
from feishu_spectator.client import ApiError, ErrorKind
from feishu_spectator.storage import Store

MEETING = '1234567890123'


def page(events, *, has_more=False, page_token=None):
    data = {'events': events, 'has_more': has_more}
    if page_token:
        data['page_token'] = page_token
    return {'code': 0, 'data': data}


def transcript_event(sentence_id='100001', text='今天讨论方案'):
    return {'activity_event_type': 'transcript_received',
            'transcript_received_items': [{
                'speaker': {'id': 'u1', 'user_type': 1, 'user_name': '张三'},
                'text': text, 'language': 'zh-CN',
                'start_time_ms': '1716699012000', 'end_time_ms': '1716699014000',
                'sentence_id': sentence_id}]}


class FakeClient:
    def __init__(self, steps):
        self.steps = list(steps)
        self.queries = []

    def events(self, query):
        self.queries.append(query)
        if not self.steps:
            return page([], has_more=False)
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


class FastClock:
    """Advances 100s per call so collector sleeps never block tests."""

    def __init__(self):
        self.now = 1_700_000_000.0

    def __call__(self):
        self.now += 100.0
        return self.now


class ParsePageTests(unittest.TestCase):
    def test_direct_activity_shape(self):
        parsed = parse_page(page([transcript_event()]), MEETING, received_at='now')
        self.assertEqual(len(parsed.items), 1)
        item = parsed.items[0]
        self.assertEqual(item.item_key, 's:100001')
        self.assertEqual(item.speaker_name, '张三')
        self.assertEqual(item.end_ms, 1716699014000)
        self.assertEqual(parsed.kinds['transcript'], 1)
        self.assertIn('events', parsed.data_keys)
        self.assertEqual(parsed.activity_types, ('transcript_received',))

    def test_nested_meeting_activity_with_string_payload(self):
        payload = json.dumps({'meeting': {'id': MEETING},
                              'activity_event_type': 'transcript_received',
                              'transcript_received_items': [
                                  {'speaker': {'id': 'u2', 'user_name': '李四'},
                                   'text': '补充', 'sentence_id': '100002'}]},
                             ensure_ascii=False)
        event = {'event_type': 'meeting.activity', 'event_id': 'e1', 'event_time': 1716699015000,
                 'payload': payload}
        parsed = parse_page(page([event]), MEETING, received_at='now')
        self.assertEqual(parsed.items[0].item_key, 's:100002')
        self.assertEqual(parsed.items[0].text, '补充')

    def test_top_level_transcript_shape(self):
        event = {'event_type': 'transcript_received', 'event_id': 'e9',
                 'payload': {'speaker': {'id': 'u3', 'user_name': '王五'}, 'text': '单句',
                             'start_time_ms': 1000, 'end_time_ms': 2000}}
        parsed = parse_page(page([event]), MEETING, received_at='now')
        item = parsed.items[0]
        self.assertEqual(item.item_key, 'e:e9:0')
        self.assertTrue(item.identity_degraded)
        self.assertEqual(item.text, '单句')

    def test_chat_and_participants(self):
        events = [
            {'activity_event_type': 'chat_received', 'chat_received_items': [
                {'operator': {'id': 'u1', 'user_name': '张三'}, 'message_id': 'om_1',
                 'message_type': 1, 'content': '大家好', 'send_time': '1716699030000'}]},
            {'activity_event_type': 'participant_joined', 'participant_joined_items': [
                {'participant': {'id': 'u2', 'user_type': 1, 'user_role': 1, 'user_name': '李四'},
                 'join_time': '1716699010000'}]},
            {'activity_event_type': 'participant_left', 'participant_left_items': [
                {'participant': {'id': 'u3', 'user_name': '王五'}, 'leave_reason': 2,
                 'leave_time': '1716700000000'}]},
            {'activity_event_type': 'magic_share_started', 'magic_share_started_items': [
                {'operator': {'id': 'u1'}, 'share_id': 's1'}]},
        ]
        parsed = parse_page(page(events), MEETING, received_at='now')
        self.assertEqual(len(parsed.items), 3)
        self.assertEqual(parsed.items[0].kind.value, 'chat')
        self.assertEqual(parsed.items[0].message_id, 'om_1')
        self.assertEqual(parsed.items[1].kind.value, 'participant_joined')
        self.assertEqual(parsed.items[2].leave_reason, 2)
        self.assertEqual(parsed.kinds['other'], 1)

    def test_has_more_requires_token(self):
        with self.assertRaises(PageFormatError):
            parse_page(page([], has_more=True), MEETING, received_at='now')

    def test_meeting_mismatch_rejected(self):
        event = {'meeting': {'id': '9999999999999'},
                 'activity_event_type': 'transcript_received',
                 'transcript_received_items': [{'speaker': {'id': 'u1'}, 'text': 'x',
                                                'sentence_id': '1'}]}
        with self.assertRaises(PageFormatError):
            parse_page(page([event]), MEETING, received_at='now')

    def test_missing_events_rejected(self):
        with self.assertRaises(PageFormatError):
            parse_page({'code': 0, 'data': {'has_more': False}}, MEETING, received_at='now')

    def test_voiceprint_user_type_passthrough(self):
        event = {'activity_event_type': 'transcript_received',
                 'transcript_received_items': [
                     {'speaker': {'id': 'vp1', 'user_type': 101, 'user_name': '声纹A'},
                      'text': '你好', 'sentence_id': '100003'}]}
        parsed = parse_page(page([event]), MEETING, received_at='now')
        self.assertEqual(parsed.items[0].speaker_name, '声纹A')


class SpectatorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'spectator.sqlite3'
        Store.initialize(self.path)
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def spectator(self, steps, *, clock=None, on_item=None):
        spectator = Spectator(self.store, MEETING, FakeClient(steps),
                              settings=ListenSettings(poll_seconds=1.0),
                              clock=clock or FastClock(), jitter=lambda: 0.0,
                              on_item=on_item)
        spectator.should_stop = lambda: not spectator.client.steps
        return spectator

    def test_incremental_paging_and_checkpoint(self):
        spectator = self.spectator([
            page([transcript_event('1')], has_more=True, page_token='t1'),
            page([transcript_event('2')], has_more=False, page_token='t2'),
        ])
        summary = spectator.poll_once()
        self.assertEqual(summary['pages'], 2)
        self.assertEqual(summary['new_items'], 2)
        self.assertEqual(self.store.checkpoint(MEETING)['page_token'], 't2')
        self.assertNotIn('page_token', spectator.client.queries[0])
        self.assertEqual(spectator.client.queries[1].get('page_token'), 't1')

    def test_cursor_resumes_after_restart(self):
        first = self.spectator([page([transcript_event('1')], has_more=True, page_token='t1'),
                                page([], has_more=False, page_token='t1')])
        first.poll_once()
        self.assertEqual(self.store.checkpoint(MEETING)['page_token'], 't1')
        second = self.spectator([page([transcript_event('2')], has_more=False, page_token='t2')])
        second.poll_once()
        self.assertEqual(second.client.queries[0].get('page_token'), 't1')
        self.assertEqual(self.store.checkpoint(MEETING)['page_token'], 't2')
        self.assertEqual(self.store.statistics(MEETING)['stored_items']['transcript'], 2)

    def test_repull_is_idempotent(self):
        first = self.spectator([page([transcript_event('1')], has_more=False)])
        summary_one = first.poll_once()
        second = self.spectator([page([transcript_event('1')], has_more=False)])
        summary_two = second.poll_once()
        self.assertEqual(summary_one['new_items'], 1)
        self.assertEqual(summary_two['new_items'], 0)
        self.assertEqual(summary_two['duplicates'], 1)

    def test_page_token_loop_detected(self):
        spectator = self.spectator([
            page([transcript_event('1')], has_more=True, page_token='t1'),
            page([transcript_event('2')], has_more=True, page_token='t1'),
        ])
        with self.assertRaises(PageFormatError):
            spectator.poll_once()

    def test_denied_is_terminal_presence_state(self):
        spectator = self.spectator([ApiError(ErrorKind.DENIED, http_status=403)])
        with self.assertRaises(ListenerStopped) as ctx:
            spectator.run()
        self.assertEqual(ctx.exception.state, 'permission_or_presence')

    def test_token_error_after_replay_is_terminal_reauthorization(self):
        spectator = self.spectator([ApiError(ErrorKind.TOKEN, http_status=401)])
        with self.assertRaises(ListenerStopped) as ctx:
            spectator.run()
        self.assertEqual(ctx.exception.state, 'reauthorization_required')

    def test_parameter_error_resets_cursor_once(self):
        establish = self.spectator([page([transcript_event('1')], has_more=True, page_token='t1'),
                                    page([], has_more=False, page_token='t1')])
        establish.poll_once()
        self.assertIsNotNone(self.store.checkpoint(MEETING)['page_token'])
        spectator = self.spectator([
            ApiError(ErrorKind.PARAMETER, code=120001),
            page([transcript_event('1'), transcript_event('2')], has_more=False),
        ])
        spectator.run()
        checkpoint = self.store.checkpoint(MEETING)
        self.assertEqual(checkpoint['cursor_reset_count'], 1)
        self.assertIsNone(checkpoint['page_token'])
        self.assertIn('page_token', spectator.client.queries[0])
        self.assertNotIn('page_token', spectator.client.queries[1])
        self.assertEqual(self.store.statistics(MEETING)['stored_items']['transcript'], 2)

    def test_second_parameter_error_is_terminal(self):
        establish = self.spectator([page([transcript_event('1')], has_more=True, page_token='t1'),
                                    page([], has_more=False, page_token='t1')])
        establish.poll_once()
        spectator = self.spectator([
            ApiError(ErrorKind.PARAMETER, code=120001),
            ApiError(ErrorKind.PARAMETER, code=120001),
        ])
        with self.assertRaises(ListenerStopped):
            spectator.run()
        self.assertEqual(self.store.checkpoint(MEETING)['cursor_reset_count'], 1)

    def test_recoverable_error_backs_off_and_continues(self):
        establish = self.spectator([page([transcript_event('1')], has_more=True, page_token='t1'),
                                    page([], has_more=False, page_token='t1')])
        establish.poll_once()
        spectator = self.spectator([
            ApiError(ErrorKind.NETWORK),
            page([transcript_event('2')], has_more=False),
        ])
        spectator.run()
        self.assertEqual(self.store.statistics(MEETING)['stored_items']['transcript'], 2)

    def test_run_stops_when_requested(self):
        spectator = self.spectator([page([], has_more=False)])
        spectator.should_stop = lambda: True
        spectator.run()  # returns without polling

    def test_on_item_emitted_after_commit(self):
        seen = []
        spectator = self.spectator([page([transcript_event('1')], has_more=False)],
                                   on_item=seen.append)
        spectator.poll_once()
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0].text, '今天讨论方案')

    def test_latency_samples_recorded_for_new_captions(self):
        clock = FastClock()
        clock.now = 1_800_000_000.0  # later than the item's 2024 end_time
        spectator = self.spectator([page([transcript_event('1')], has_more=False)], clock=clock)
        spectator.poll_once()
        samples = self.store.checkpoint(MEETING)['latency_samples']
        self.assertEqual(len(samples), 1)
        # The fake clock is far past the 2024 end_time; the sample is the gap in seconds.
        from feishu_spectator.collector import latency_summary
        summary = latency_summary(samples)
        self.assertEqual(summary['count'], 1)
        self.assertGreater(summary['median'], 0)
        # Duplicates must not add samples.
        again = self.spectator([page([transcript_event('1')], has_more=False)])
        again.poll_once()
        self.assertEqual(len(self.store.checkpoint(MEETING)['latency_samples']), 1)

    def test_latency_summary_bounds(self):
        from feishu_spectator.collector import latency_summary
        self.assertEqual(latency_summary([]), {'count': 0})
        self.assertEqual(latency_summary([None, 'x', -1]), {'count': 0})
        summary = latency_summary([1, 2, 3, 4, 5, 6, 7, 8, 9, 100])
        self.assertEqual(summary['count'], 10)
        self.assertEqual(summary['median'], 5.5)
        self.assertEqual(summary['max'], 100)


if __name__ == '__main__':
    unittest.main()
