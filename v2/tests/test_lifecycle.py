from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from feishu_meeting_adviser.client import ApiError, ErrorKind
from feishu_meeting_adviser.config import ConfigError, load_config
from feishu_meeting_adviser.lifecycle import LifecycleError, MeetingSource, SessionManager
from feishu_meeting_adviser.models import ChangeKind, SourceState
from feishu_meeting_adviser.storage import Store
from test_collector import event, page

MID = '987654321012345678'


class BotAPI:
    def __init__(self):
        self.joins = self.leaves = self.event_calls = 0
        self.failure = None
    def join(self, meeting_no, password):
        self.joins += 1
        if self.failure:
            raise self.failure
        return {'data':{'meeting':{'id':MID,'meeting_no':meeting_no},'join_user':{'id':'synthetic-bot'}}}
    def leave(self, meeting_id):
        self.leaves += 1
        if self.failure:
            raise self.failure
        return {'data':{}}
    def events(self, query):
        self.event_calls += 1
        return page([event()])


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'config.yaml'
        self.path.write_text('app:\n  id: synthetic\n  secret: private-secret\nlookup:\n  meeting_no: "012345678"\nprobe:\n  timeout_seconds: 1\n')
        self.path.chmod(0o600)
        self.config = replace(load_config(self.path), poll_seconds=0.01)
        self.store = Store(self.config.data_directory / 'source.sqlite3')
        self.addCleanup(self.store.close)
        self.api = BotAPI()
        self.manager = SessionManager(self.store, self.config, self.api, clock=lambda:1_800_000_000)

    def test_legacy_credentials_read_without_mutation_and_bad_config_hidden(self):
        original = self.path.read_bytes()
        config = load_config(self.path)
        self.assertEqual(config.meeting_no, '012345678')
        self.assertEqual(self.path.read_bytes(), original)
        self.path.write_text('private-secret: [broken')
        with self.assertRaises(ConfigError) as caught:
            load_config(self.path)
        self.assertNotIn('private-secret', str(caught.exception))

    def test_join_and_leave_repeat_without_duplicate_api_operations(self):
        one = self.manager.join()
        self.assertEqual(self.manager.join(), one)
        self.assertEqual(self.api.joins, 1)
        self.manager.leave()
        self.manager.leave()
        self.assertEqual(self.api.leaves, 1)
        self.assertEqual(self.manager.session()['phase'], 'left')

    def test_uncertain_join_and_leave_require_explicit_reconciliation(self):
        self.api.failure = ApiError(ErrorKind.NETWORK)
        with self.assertRaises(ApiError):
            self.manager.join()
        with self.assertRaises(LifecycleError):
            self.manager.join()
        self.assertEqual(self.api.joins, 1)
        self.manager.reset_uncertain(confirmed_bot_left=True)
        self.api.failure = None
        self.manager.join()
        self.api.failure = ApiError(ErrorKind.NETWORK)
        with self.assertRaises(ApiError):
            self.manager.leave()
        with self.assertRaises(LifecycleError):
            self.manager.leave()
        self.api.failure = ApiError(ErrorKind.BOT_ABSENT, code=121110)
        self.manager.leave(retry_uncertain=True)
        self.assertEqual(self.manager.session()['phase'], 'left')

    def test_switch_meeting_requires_leaving_verified_old_session(self):
        self.manager.join()
        other = SessionManager(self.store, replace(self.config, meeting_no='123456789'), self.api)
        with self.assertRaises(LifecycleError):
            other.join()
        other.leave()  # Explicit leave always targets the verified saved session.
        self.assertEqual(self.api.leaves, 1)
        other.join()
        self.assertEqual(other.session()['meeting_no'], '123456789')

    def test_source_thread_can_persist_and_caller_read_and_ack(self):
        self.manager.join()
        source = MeetingSource(self.store, self.manager, self.api, clock=lambda:1_800_000_000)
        self.addCleanup(source.stop)
        source.start(max_successful_cycles=1)
        source.thread.join(timeout=5)
        self.assertFalse(source.running)
        self.assertIsNone(source.failure)
        rows = source.read('qa')
        self.assertEqual(len([r for r in rows if r.caption]), 1)
        source.acknowledge('qa', rows[-1].sequence)
        self.assertEqual(source.read('qa'), [])
        self.assertEqual(source.state().state, SourceState.STOPPED)
        self.assertEqual(self.manager.session()['phase'], 'joined')
        self.assertEqual(self.api.leaves, 0)
        source.start(max_successful_cycles=1)
        source.thread.join(timeout=5)
        self.assertEqual(self.api.event_calls, 2)
        risks = self.store.db.execute('SELECT count(*) FROM risks WHERE meeting_id=?', (MID,)).fetchone()[0]
        self.assertGreater(risks, 0)

    def test_application_lock_blocks_join_leave_and_second_collector(self):
        self.manager.join()
        entered = threading.Event()
        released = threading.Event()
        api = self.api
        def blocked(query):
            entered.set()
            released.wait(3)
            return page()
        api.events = blocked
        source = MeetingSource(self.store, self.manager, api, clock=lambda:1_800_000_000)
        self.addCleanup(source.stop)
        source.start(max_successful_cycles=1)
        self.assertTrue(entered.wait(2))
        try:
            source.start()  # Same instance is idempotent.
            with self.assertRaises(LifecycleError):
                self.manager.leave()
            second = MeetingSource(self.store, self.manager, api, clock=lambda:1_800_000_000)
            with self.assertRaises(LifecycleError):
                second.start()
        finally:
            released.set()
            source.stop()
        self.manager.leave()
        self.assertEqual(self.api.leaves, 1)

    def test_changed_config_stops_before_request(self):
        self.manager.join()
        source = MeetingSource(self.store, self.manager, self.api, clock=lambda:1_800_000_000)
        self.addCleanup(source.stop)
        self.path.write_text(self.path.read_text() + '# changed\n')
        source.start()
        source.thread.join(timeout=3)
        self.assertFalse(source.running)
        self.assertEqual(source.state().reason, 'configuration_changed')
        self.assertEqual(self.api.event_calls, 0)

    def test_session_change_and_bot_departure_are_not_meeting_end(self):
        self.manager.join()
        source = MeetingSource(self.store, self.manager, self.api, clock=lambda:1_800_000_000)
        self.addCleanup(source.stop)
        left = {'event_type':'meeting.activity', 'payload':{'activity_event_type':'participant_left',
                'participant_left_items':[{'participant':{'id':'synthetic-bot'}, 'leave_time':'1800000000500'}]}}
        self.api.events = lambda query: page([event(), left])
        source.start()
        source.thread.join(timeout=3)
        self.assertEqual(source.state().state, SourceState.ACTION_REQUIRED)
        self.assertNotEqual(source.state().state, SourceState.ENDED)
        self.assertEqual(len([r for r in source.read('qa') if r.caption]), 1)
        self.api.events = lambda query: page()
        current = self.manager.session()
        self.store.save_session(self.manager.app_key, {**current, 'phase':'left'})
        self.assertEqual(source._before_cycle(self.store), 'session_changed')

    def test_manual_conflict_resolution_updates_context_and_preserves_other_risks(self):
        self.manager.join()
        source = MeetingSource(self.store, self.manager, self.api, clock=lambda:1_800_000_000)
        source.start(max_successful_cycles=1)
        source.thread.join(timeout=3)
        original = self.store.caption(MID, 's:s1')
        self.store.commit_page(MID, [replace(original, text='manually reviewed candidate')], self.store.checkpoint(MID))
        conflict = next(r for r in source.read('qa') if r.kind == ChangeKind.CONFLICT)
        self.assertTrue(self.store.resolve_conflict(MID, conflict.delivery_id, at='now'))
        self.assertFalse(self.store.resolve_conflict(MID, conflict.delivery_id, at='now'))
        self.assertEqual(self.store.caption(MID, 's:s1').text, 'manually reviewed candidate')
        self.assertEqual(self.store.db.execute('SELECT count(*) FROM conflicts WHERE resolved=0').fetchone()[0], 0)

    def test_cli_join_status_consume_leave_use_local_fake_api(self):
        from contextlib import redirect_stdout
        import io
        from feishu_meeting_adviser.__main__ import main
        output = io.StringIO()
        with patch('feishu_meeting_adviser.__main__.FeishuClient', return_value=self.api), redirect_stdout(output):
            self.assertEqual(main(['--config', str(self.path), 'join']), 0)
            self.assertEqual(main(['--config', str(self.path), 'status']), 0)
            # No network request for status and no credentials in default output.
            self.assertEqual(self.api.event_calls, 0)
            self.assertEqual(main(['--config', str(self.path), 'leave']), 0)
        self.assertNotIn('private-secret', output.getvalue())
        self.assertNotIn(MID, output.getvalue())
