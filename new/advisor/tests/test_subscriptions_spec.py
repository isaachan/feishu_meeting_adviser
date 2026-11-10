"""Plan-4.1 acceptance: subscription state machine against STATE_MACHINES.md.

Every legal transition (T1-T5), every rejection row, and the invariants —
including global #rN monotonicity and the organizer-row equivalent.
"""
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from advisor.errors import StateError, StorageError
from advisor.store import SessionStore

NOW = 2_000_000.0


class StateMachineSpecTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'adviser.sqlite3'
        SessionStore.initialize(self.path)
        self.store = SessionStore(self.path)
        self.addCleanup(self.store.close)
        self.now = NOW
        self.session = self.store.create_session(
            owner_open_id='ou_A', meeting_no='123456789', title='周会',
            event_calendar_id='cal_x', event_id='evt_y', now=self.now)
        self.sid = self.session['session_id']

    def subscribe(self, subscriber='ou_B'):
        return self.store.create_subscription_request(self.sid, subscriber, now=self.now)

    # T1
    def test_T1_request_created_in_recording_session(self):
        row = self.subscribe()
        self.assertEqual((row['state'], row['existing']), ('requested', False))
        self.assertIsInstance(row['request_no'], int)

    def test_T1_rejected_when_session_not_recording(self):
        for phase in ('draining', 'ended'):
            self.store.set_phase(self.sid, phase, now=self.now)
            with self.assertRaises(StateError):
                self.subscribe(subscriber=f'ou_{phase}')
            if phase == 'draining':  # reset for the next loop via fresh session
                self.sid = self.store.create_session(
                    owner_open_id='ou_A', meeting_no='999000111', title='x',
                    event_calendar_id='c', event_id='e', now=self.now)['session_id']

    # T2 / T3
    def test_T2_approve_and_T3_deny(self):
        approved = self.subscribe()
        self.assertEqual(self.store.decide_subscription(
            approved['request_no'], 'ou_A', 'approve', now=self.now)['state'], 'approved')
        denied = self.subscribe('ou_C')
        self.assertEqual(self.store.decide_subscription(
            denied['request_no'], 'ou_A', 'deny', now=self.now)['state'], 'denied')

    # T4
    def test_T4_revoke_only_from_approved_and_cancels_pending_im(self):
        request = self.subscribe()
        self.store.decide_subscription(request['request_no'], 'ou_A', 'approve', now=self.now)
        with self.store.unit_of_work() as tx:
            tx.outbox.save_reply(
                message_uuid='u-1', session_id=self.sid, content='回答',
                evidence='{}', generation=1, created_ns=1, context_version=None,
                recipients=('ou_A', 'ou_B'), now=self.now)
        result = self.store.revoke_subscription(request['request_no'], 'ou_A', now=self.now)
        self.assertEqual(result['state'], 'revoked')
        pending = [r for r in self.store.db.execute(
            "SELECT recipient_id,state FROM outbox WHERE channel='im'")]
        states = {r['recipient_id']: r['state'] for r in pending}
        self.assertEqual(states.get('ou_B'), 'cancelled')
        self.assertEqual(states.get('ou_A'), 'pending')

    # T5
    def test_T5_denied_reapplies_as_new_request_with_previous(self):
        first = self.subscribe()
        self.store.decide_subscription(first['request_no'], 'ou_A', 'deny', now=self.now)
        second = self.subscribe()
        self.assertEqual(second['state'], 'requested')
        self.assertEqual(second['previous'], {'request_no': first['request_no'],
                                              'state': 'denied'})
        self.assertGreater(second['request_no'], first['request_no'])

    # rejection rows
    def test_approved_reapply_returns_existing(self):
        request = self.subscribe()
        self.store.decide_subscription(request['request_no'], 'ou_A', 'approve', now=self.now)
        again = self.subscribe()
        self.assertEqual((again['state'], again['existing']), ('approved', True))
        self.assertEqual(again['request_no'], request['request_no'])

    def test_revoked_is_terminal_everywhere(self):
        request = self.subscribe()
        self.store.decide_subscription(request['request_no'], 'ou_A', 'approve', now=self.now)
        self.store.revoke_subscription(request['request_no'], 'ou_A', now=self.now)
        with self.assertRaises(StateError):
            self.subscribe()
        with self.assertRaises(StateError):
            self.store.decide_subscription(request['request_no'], 'ou_A', 'approve',
                                           now=self.now)
        with self.assertRaises(StateError):
            self.store.decide_subscription(request['request_no'], 'ou_A', 'deny',
                                           now=self.now)
        with self.assertRaises(StateError):
            self.store.revoke_subscription(request['request_no'], 'ou_A', now=self.now)

    def test_decide_idempotent_echo_on_settled_states(self):
        request = self.subscribe()
        self.store.decide_subscription(request['request_no'], 'ou_A', 'approve', now=self.now)
        echo = self.store.decide_subscription(request['request_no'], 'ou_A', 'deny',
                                              now=self.now)
        self.assertEqual(echo['state'], 'approved')

    def test_owner_gates_and_invalid_decisions(self):
        request = self.subscribe()
        with self.assertRaises(StateError):
            self.store.decide_subscription(request['request_no'], 'ou_X', 'approve',
                                           now=self.now)
        with self.assertRaises(StorageError):
            self.store.decide_subscription(request['request_no'], 'ou_A', 'maybe',
                                           now=self.now)

    def test_request_numbers_global_and_monotonic_across_sessions(self):
        other = self.store.create_session(
            owner_open_id='ou_Z', meeting_no='555000555', title='y',
            event_calendar_id='c2', event_id='e2', now=self.now)
        first = self.subscribe()
        second = self.store.create_subscription_request(
            other['session_id'], 'ou_Q', now=self.now)
        self.assertGreater(second['request_no'], first['request_no'])

    def test_organizer_row_equivalent_owner_always_recipient(self):
        with self.store.unit_of_work() as tx:
            recipients = tx.subscriptions.approved_recipients(self.sid, 'ou_A')
        self.assertIn('ou_A', recipients)
        self.assertNotIn('ou_B', recipients)
        request = self.subscribe()
        self.store.decide_subscription(request['request_no'], 'ou_A', 'approve', now=self.now)
        with self.store.unit_of_work() as tx:
            recipients = tx.subscriptions.approved_recipients(self.sid, 'ou_A')
        self.assertEqual(set(recipients), {'ou_A', 'ou_B'})

    def test_registry_resolves_only_recording_sessions(self):
        found = self.store.active_by_meeting_no('123456789')
        self.assertEqual(found['session_id'], self.sid)
        self.store.set_phase(self.sid, 'draining', now=self.now)
        self.assertIsNone(self.store.active_by_meeting_no('123456789'))


if __name__ == '__main__':
    unittest.main()
