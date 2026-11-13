"""LoginFlow state machine with a scripted fake client."""
import unittest

from macos_gui.api import ApiError, NetworkError
from macos_gui.login import LoginFlow


class FakeClient:
    def __init__(self):
        self.poll_results = []
        self.start_result = {'login_url': 'https://accounts.feishu.cn/x',
                             'device_code': 'dc-1', 'expires_at': 1.0}

    def login_start(self):
        return dict(self.start_result)

    def login_poll(self, device_code):
        result = self.poll_results.pop(0)
        if isinstance(result, Exception):
            raise result
        return dict(result)


class LoginFlowTests(unittest.TestCase):
    def test_pending_then_completed(self):
        client = FakeClient()
        client.poll_results = [{'status': 'pending'},
                               {'status': 'pending'},
                               {'status': 'completed', 'device_token': 'dt-9',
                                'user': {'open_id': 'ou_A', 'name': 'K'}}]
        flow = LoginFlow(client)
        start = flow.begin()
        self.assertEqual(start['device_code'], 'dc-1')
        self.assertTrue(flow.in_progress)
        self.assertEqual(flow.tick()['state'], 'awaiting_scan')
        self.assertEqual(flow.tick()['state'], 'awaiting_scan')
        done = flow.tick()
        self.assertEqual(done, {'state': 'completed', 'device_token': 'dt-9',
                                'user': {'open_id': 'ou_A', 'name': 'K'}})
        self.assertFalse(flow.in_progress)

    def test_expired_is_terminal_and_restarts_on_begin(self):
        client = FakeClient()
        client.poll_results = [ApiError(410, 'expired', 'x')]
        flow = LoginFlow(client)
        flow.begin()
        self.assertEqual(flow.tick()['state'], 'expired')
        self.assertFalse(flow.in_progress)
        client.poll_results = [{'status': 'pending'}]
        flow.begin()
        self.assertEqual(flow.tick()['state'], 'awaiting_scan')

    def test_consumed_device_code_maps_to_expired(self):
        client = FakeClient()
        client.poll_results = [ApiError(404, 'unknown_device', 'x')]
        flow = LoginFlow(client)
        flow.begin()
        self.assertEqual(flow.tick()['state'], 'expired')

    def test_network_and_auth_unavailable_are_transient(self):
        client = FakeClient()
        client.poll_results = [NetworkError('down'),
                               ApiError(409, 'auth_unavailable', 'x'),
                               {'status': 'pending'}]
        flow = LoginFlow(client)
        flow.begin()
        self.assertEqual(flow.tick()['state'], 'unavailable')
        self.assertEqual(flow.tick()['state'], 'unavailable')
        self.assertTrue(flow.in_progress)
        self.assertEqual(flow.tick()['state'], 'awaiting_scan')

    def test_completed_without_token_is_expired(self):
        client = FakeClient()
        client.poll_results = [{'status': 'completed'}]
        flow = LoginFlow(client)
        flow.begin()
        self.assertEqual(flow.tick()['state'], 'expired')

    def test_tick_without_begin_raises(self):
        with self.assertRaises(RuntimeError):
            LoginFlow(FakeClient()).tick()


if __name__ == '__main__':
    unittest.main()
