from pathlib import Path
import tempfile
import unittest
from feishu_meeting_adviser.collector import Collector, PageFormatError, ScanSettings, parse_page
from feishu_meeting_adviser.models import ChangeKind, IngestMode
from feishu_meeting_adviser.storage import Store


def event(sid='s1', text='synthetic', timestamp='2026-10-13T00:00:00.100Z', *, nested=False, **item):
    payload = {'transcript_received_items': [{'sentence_id': sid, 'text': text, **item}]}
    if nested:
        payload['activity_event_type'] = 'transcript_received'
        import json
        payload = json.dumps(payload)
    return {'event_type': 'meeting.activity' if nested else 'transcript_received', 'event_id': 'synthetic-event-' + str(sid),
            'event_time': timestamp, 'payload': payload}


def page(events=(), more=False, cursor=None):
    return {'data': {'events': list(events), 'has_more': more, 'page_token': cursor}}


class FakeAPI:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.queries = []
    def events(self, query):
        self.queries.append(dict(query))
        return next(self.pages)


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'data' / 'source.db'
        self.store = Store(self.path)
        self.addCleanup(lambda: self.store.close())
        self.now = [1_800_000_000]

    def collector(self, api, **settings):
        return Collector(self.store, 'm1', api, settings=ScanSettings(**settings), clock=lambda: self.now[0])

    def test_multicycle_pagination_restart_freezes_query_bounds(self):
        api = FakeAPI([page([event()], True, 'next')])
        collector = self.collector(api, max_pages_per_cycle=1)
        collector.cycle()
        checkpoint = self.store.checkpoint('m1')
        self.now[0] += 60
        self.store.close()
        self.store = Store(self.path)
        continued = FakeAPI([page([event(), event('s2')])])
        self.collector(continued, page_size=20).cycle()
        self.assertEqual(continued.queries[0]['start_time'], api.queries[0]['start_time'])
        self.assertEqual(continued.queries[0]['end_time'], api.queries[0]['end_time'])
        self.assertEqual(continued.queries[0]['page_size'], 100)
        self.assertEqual(continued.queries[0]['page_token'], 'next')
        self.assertEqual(len(self.store.read('m1', 'qa')), 2)

    def test_empty_page_moves_completed_scan_boundary_not_event_maximum(self):
        api = FakeAPI([page(), page()])
        collector = self.collector(api)
        collector.cycle()
        self.now[0] += 5
        collector.cycle()
        self.assertEqual(int(api.queries[1]['start_time']), 1_800_000_000 - 120)
        self.assertEqual(self.store.checkpoint('m1')['completed_until'], self.now[0])
        self.assertIsNone(self.store.current_state('m1'))

    def test_reconciliation_can_pick_up_caption_older_than_overlap(self):
        api = FakeAPI([page(), page([event('late')])])
        collector = self.collector(api)
        collector.cycle()
        self.now[0] += 61
        collector.cycle()
        self.assertEqual(api.queries[1]['start_time'], api.queries[0]['start_time'])
        row = self.store.read('m1', 'qa')[0]
        self.assertEqual(row.caption.mode, IngestMode.BACKFILL)
        self.assertTrue(row.historical)

    def test_same_second_unknown_revision_is_preserved_as_conflict(self):
        api = FakeAPI([page([event(text='original')]), page([event(text='changed', timestamp='2026-10-13T00:00:00.900Z')])])
        collector = self.collector(api)
        collector.cycle()
        collector.cycle()
        rows = self.store.read('m1', 'qa')
        self.assertIn(ChangeKind.CONFLICT, [r.kind for r in rows])
        self.assertEqual(self.store.caption('m1', 's:s1').text, 'original')

    def test_corrupt_page_rolls_back_and_cursor_loop_stops(self):
        api = FakeAPI([page([event()], True, 'A'), page([], True, 'B'), page([], True, 'A')])
        collector = self.collector(api, max_pages_per_cycle=1)
        collector.cycle()
        collector.cycle()
        with self.assertRaises(PageFormatError):
            collector.cycle()
        self.assertEqual(self.store.checkpoint('m1')['cursor'], 'B')
        malformed = FakeAPI([page([event('s2'), {'event_type': 'transcript_received', 'payload': {'text': 1}}])])
        collector.client = malformed
        with self.assertRaises(PageFormatError):
            collector.cycle()
        self.assertIsNone(self.store.caption('m1', 's:s2'))

    def test_nested_direct_metadata_and_degraded_identity(self):
        response = page([event(nested=True, end_time_ms='2026-10-13T00:00:00.999999999Z', speaker={'id':'synthetic-user', 'user_name':'synthetic'}),
                         event(None, speaker={'id': 'other-user'})])
        parsed = parse_page(response, 'm1', received_at='now', mode=IngestMode.INITIAL)
        self.assertEqual(len(parsed.captions), 2)
        self.assertIsNotNone(parsed.captions[0].end_ns)
        self.assertEqual(parsed.fallback_identities, 1)

    def test_reset_cursor_preserves_window_and_acknowledgements(self):
        collector = self.collector(FakeAPI([page([event()], True, 'expired')]), max_pages_per_cycle=1)
        collector.cycle()
        row = self.store.read('m1', 'qa')[0]
        self.store.acknowledge('m1', 'qa', row.sequence)
        original = self.store.checkpoint('m1')
        collector.reset_cursor('cursor_invalid')
        reset = self.store.checkpoint('m1')
        self.assertEqual(reset['query_start'], original['query_start'])
        self.assertEqual(reset['query_end'], original['query_end'])
        self.assertIsNone(reset['cursor'])
        self.assertEqual([row.kind for row in self.store.read('m1', 'qa')], [ChangeKind.STATUS])
