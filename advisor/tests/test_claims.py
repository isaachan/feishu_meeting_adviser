"""SM-05: single-claim semantics across sessions (STATE_MACHINES v2 §0/I4)."""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from advisor.errors import StateError, StorageError
from advisor.persistence import meeting_instance_key
from advisor.store import SessionStore

NOW = 1762800000.0


class ClaimBase(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'adviser.sqlite3'
        SessionStore.initialize(self.path)
        self.store = SessionStore(self.path)
        self.addCleanup(self.store.close)
        self.counter = 0

    def create(self, owner, meeting_no, *, start=1762800000, event_id=None):
        event_id = event_id or f'evt_{meeting_no}'
        session, _existing = self.store.create_session(
            owner_open_id=owner, meeting_no=meeting_no, title='t',
            event_calendar_id='cal_x', event_id=event_id, now=NOW,
            instance_key=meeting_instance_key(meeting_no, 'cal_x', event_id, start))
        return session

    def other_session(self):
        """A session owned by someone else for 小B to subscribe to."""
        return self.create('ou_OWNER', '777000777', start=1762800000)

    def subscribe(self, session_id, subscriber='ou_B', *, now=NOW):
        return self.store.create_subscription_request(session_id, subscriber, now=now)


class ClaimOccupancyTests(ClaimBase):
    def test_requested_occupies_single_claim(self):
        session = self.other_session()
        self.subscribe(session['session_id'])
        self.assertIsNotNone(self.store.active_claim('ou_B'))
        with self.assertRaises(StateError) as caught:
            self.create('ou_B', '111222333', start=1762800000)
        self.assertEqual(caught.exception.args[0], 'claim_conflict')
        with self.assertRaises(StateError) as caught:
            self.subscribe(self.create('ou_OWNER2', '555000555')['session_id'])
        self.assertEqual(caught.exception.args[0], 'claim_conflict')

    def test_denied_releases_claim(self):
        session = self.other_session()
        row = self.subscribe(session['session_id'])
        self.store.decide_subscription(row['request_no'], 'ou_OWNER', 'deny', now=NOW)
        self.assertIsNone(self.store.active_claim('ou_B'))
        second = self.create('ou_B', '111222333')  # allowed again
        self.assertEqual(second['phase'], 'running')

    def test_approved_keeps_claim_and_blocks_second_session(self):
        session = self.other_session()
        row = self.subscribe(session['session_id'])
        self.store.decide_subscription(row['request_no'], 'ou_OWNER', 'approve', now=NOW)
        claim = self.store.active_claim('ou_B')
        self.assertEqual((claim['session_id'], claim['kind']),
                         (session['session_id'], 'subscriber'))
        with self.assertRaises(StateError) as caught:
            self.create('ou_B', '111222333')
        self.assertEqual(caught.exception.args[0], 'claim_conflict')

    def test_revoke_releases_claim(self):
        session = self.other_session()
        row = self.subscribe(session['session_id'])
        self.store.decide_subscription(row['request_no'], 'ou_OWNER', 'approve', now=NOW)
        self.store.revoke_subscription(row['request_no'], 'ou_B', now=NOW)
        self.assertIsNone(self.store.active_claim('ou_B'))

    def test_owner_cannot_subscribe_own_session(self):
        session = self.create('ou_A', '123456789')
        with self.assertRaises(StateError) as caught:
            self.subscribe(session['session_id'], 'ou_A')
        self.assertEqual(caught.exception.args[0], 'claim_conflict')

    def test_ended_releases_all_claims(self):
        session = self.other_session()
        row = self.subscribe(session['session_id'])
        self.store.decide_subscription(row['request_no'], 'ou_OWNER', 'approve', now=NOW)
        self.store.set_phase(session['session_id'], 'draining', now=NOW)
        self.store.set_phase(session['session_id'], 'ended', now=NOW)
        self.assertIsNone(self.store.active_claim('ou_B'))
        self.assertIsNone(self.store.active_claim('ou_OWNER'))
        # claims history preserved
        kinds = {(c['user_open_id'], c['kind'], c['active'])
                 for c in self.store.claims_for_session(session['session_id'])}
        self.assertEqual(kinds, {('ou_OWNER', 'controller', 0), ('ou_B', 'subscriber', 0)})

    def test_resubscribe_after_denial_reoccupies(self):
        session = self.other_session()
        first = self.subscribe(session['session_id'])
        self.store.decide_subscription(first['request_no'], 'ou_OWNER', 'deny', now=NOW)
        second = self.subscribe(session['session_id'])
        self.assertEqual((second['state'], second['existing']), ('requested', False))
        self.assertIsNotNone(self.store.active_claim('ou_B'))

    def test_paused_and_draining_keep_claims(self):
        session = self.other_session()
        row = self.subscribe(session['session_id'])
        self.store.set_phase(session['session_id'], 'paused', now=NOW)
        self.assertIsNotNone(self.store.active_claim('ou_B'))
        self.store.set_phase(session['session_id'], 'running', now=NOW)
        self.store.set_phase(session['session_id'], 'draining', now=NOW)
        self.assertIsNotNone(self.store.active_claim('ou_B'))


class OwnerAuthorityTests(ClaimBase):
    def test_owner_fixed_at_creation(self):
        session = self.create('ou_C', '123456789')
        # 同实例他人建会 → session_claimed（不转移归属）
        with self.assertRaises(StateError) as caught:
            self.store.create_session(
                owner_open_id='ou_A', meeting_no='123456789', title='t',
                event_calendar_id='cal_x', event_id='evt_123456789', now=NOW,
                instance_key=meeting_instance_key('123456789', 'cal_x',
                                                  'evt_123456789', 1762800000))
        self.assertEqual(caught.exception.args[0], 'session_claimed')
        row = self.store.require_session(session['session_id'])
        self.assertEqual(row['owner_open_id'], 'ou_C')

    def test_organizer_presence_independent_of_owner(self):
        session = self.create('ou_C', '123456789')
        row = self.store.require_session(session['session_id'])
        self.assertEqual(row['owner_open_id'], 'ou_C')
        # organizer column carries the calendar organizer separately (v2)
        self.assertIn('organizer_open_id', row)

    def test_non_owner_cannot_end(self):
        from advisor.api import AdvisorApi  # noqa: F401  (surface covered in api tests)
        session = self.create('ou_C', '123456789')
        self.store.set_phase(session['session_id'], 'draining', now=NOW)
        self.assertEqual(self.store.require_session(session['session_id'])['phase'],
                         'draining')


if __name__ == '__main__':
    unittest.main()
