"""Plan-4.1 acceptance: subscription state machine against STATE_MACHINES.md.

Every legal transition (T1-T5), every rejection row, and the invariants —
including global #rN monotonicity and the organizer-row equivalent.
"""
import time
import threading
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
            event_calendar_id='cal_x', event_id='evt_y', now=self.now)[0]
        self.sid = self.session['session_id']

    def subscribe(self, subscriber='ou_B'):
        return self.store.create_subscription_request(self.sid, subscriber, now=self.now)

    # T1
    def test_T1_request_created_in_running_session(self):
        row = self.subscribe()
        self.assertEqual((row['state'], row['existing']), ('requested', False))
        self.assertIsInstance(row['request_no'], int)
        self.assertEqual(row['approval_deadline_at'], self.now + 1800)

    def test_duplicate_requested_returns_same_request_no(self):
        first = self.subscribe()
        second = self.subscribe()
        self.assertTrue(second['existing'])
        self.assertEqual(first['request_no'], second['request_no'])

    def test_requested_withdraw_releases_claim_and_new_line_requires_approval(self):
        first = self.subscribe()
        result = self.store.revoke_subscription(first['request_no'], 'ou_B', now=self.now)
        self.assertEqual(result['state'], 'subscriber_cancelled')
        self.assertIsNone(self.store.active_claim('ou_B'))
        second = self.subscribe()
        self.assertGreater(second['request_no'], first['request_no'])
        self.assertEqual(second['state'], 'requested')
        self.assertEqual(second['previous']['state'], 'subscriber_cancelled')

    def test_approval_ttl_expires_and_releases_slot_even_after_restart(self):
        first = self.store.create_subscription_request(
            self.sid, 'ou_B', now=self.now, approval_ttl_s=2)
        path = self.path
        self.store.close()
        self.store = SessionStore(path)
        self.addCleanup(self.store.close)
        self.assertEqual(self.store.expire_subscription_requests(now=self.now + 2), 1)
        self.assertIsNone(self.store.active_claim('ou_B'))
        state = self.store.db.execute(
            'SELECT state,closed_by FROM subscriptions WHERE request_no=?',
            (first['request_no'],)).fetchone()
        self.assertEqual(tuple(state), ('expired', 'system'))
        with self.assertRaises(StateError):
            self.store.decide_subscription(first['request_no'], 'ou_A', 'approve',
                                           now=self.now + 3)
        new = self.store.create_subscription_request(self.sid, 'ou_B', now=self.now + 3)
        self.assertGreater(new['request_no'], first['request_no'])

    def test_expiry_and_approval_boundary_are_atomic(self):
        first = self.store.create_subscription_request(
            self.sid, 'ou_B', now=self.now, approval_ttl_s=2)
        result = self.store.decide_subscription(first['request_no'], 'ou_A', 'approve',
                                                now=self.now + 2)
        self.assertEqual(result['state'], 'expired')
        self.assertTrue(result['expired'])
        self.assertIsNone(self.store.active_claim('ou_B'))

    def test_concurrent_approve_and_withdraw_leave_single_terminal_row(self):
        first = self.subscribe()
        errors = []

        def approve():
            try:
                self.store.decide_subscription(first['request_no'], 'ou_A', 'approve',
                                               now=self.now + 1)
            except StateError as exc:
                errors.append(exc)

        def withdraw():
            try:
                self.store.revoke_subscription(first['request_no'], 'ou_B', now=self.now + 1)
            except StateError as exc:
                errors.append(exc)

        a, b = threading.Thread(target=approve), threading.Thread(target=withdraw)
        a.start(); b.start(); a.join(); b.join()
        row = self.store.db.execute(
            'SELECT state FROM subscriptions WHERE request_no=?',
            (first['request_no'],)).fetchone()
        self.assertEqual(row['state'], 'subscriber_cancelled')
        self.assertIsNone(self.store.active_claim('ou_B'))
        self.assertFalse(errors)

    def test_T1_rejected_when_session_not_active(self):
        for phase in ('draining', 'ended'):
            self.store.set_phase(self.sid, 'draining', now=self.now)
            if phase == 'ended':
                self.store.set_phase(self.sid, 'ended', now=self.now)
            with self.assertRaises(StateError):
                self.subscribe(subscriber=f'ou_{phase}')
            if phase == 'draining':  # v2: claims survive draining -> end first
                self.store.set_phase(self.sid, 'ended', now=self.now)
                self.sid = self.store.create_session(
                    owner_open_id='ou_A', meeting_no='999000111', title='x',
                    event_calendar_id='c', event_id='e', now=self.now)[0]['session_id']

    def test_T1_accepted_when_session_paused(self):
        self.store.set_phase(self.sid, 'paused', now=self.now)
        row = self.subscribe()
        self.assertEqual(row['state'], 'requested')

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
        self.assertEqual(result['state'], 'owner_revoked')
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

    def test_owner_revoked_row_terminal_but_new_request_allowed(self):
        request = self.subscribe()
        self.store.decide_subscription(request['request_no'], 'ou_A', 'approve', now=self.now)
        self.store.revoke_subscription(request['request_no'], 'ou_A', now=self.now)
        second = self.subscribe()
        self.assertGreater(second['request_no'], request['request_no'])
        self.assertEqual(second['previous']['state'], 'owner_revoked')
        self.assertEqual(self.store.decide_subscription(
            request['request_no'], 'ou_A', 'approve', now=self.now)['state'],
            'owner_revoked')
        self.assertEqual(self.store.revoke_subscription(
            request['request_no'], 'ou_A', now=self.now)['state'], 'owner_revoked')

    def test_decide_idempotent_echo_on_settled_states(self):
        request = self.subscribe()
        self.store.decide_subscription(request['request_no'], 'ou_A', 'approve', now=self.now)
        echo = self.store.decide_subscription(request['request_no'], 'ou_A', 'deny',
                                              now=self.now)
        self.assertEqual(echo['state'], 'approved')

    def test_subscriber_may_revoke_own_subscription_only(self):
        request = self.subscribe()
        self.store.decide_subscription(request['request_no'], 'ou_A', 'approve', now=self.now)
        # a third party cannot revoke
        with self.assertRaises(StateError):
            self.store.revoke_subscription(request['request_no'], 'ou_X', now=self.now)
        # the subscriber themself can (opt-out)
        result = self.store.revoke_subscription(request['request_no'], 'ou_B', now=self.now)
        self.assertEqual(result['state'], 'subscriber_cancelled')

    def test_subscriptions_for_subscriber_listing(self):
        request = self.subscribe()
        self.store.decide_subscription(request['request_no'], 'ou_A', 'approve', now=self.now)
        rows = self.store.subscriptions_for_subscriber('ou_B')
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['meeting_no'], rows[0]['state']), ('123456789', 'approved'))

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
            event_calendar_id='c2', event_id='e2', now=self.now)[0]
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

    def test_registry_resolves_only_active_sessions(self):
        found = self.store.active_sessions_by_meeting_no('123456789')
        self.assertEqual([s['session_id'] for s in found], [self.sid])
        self.store.set_phase(self.sid, 'draining', now=self.now)
        self.assertEqual(self.store.active_sessions_by_meeting_no('123456789'), [])


if __name__ == '__main__':
    unittest.main()
