"""Tool protocol tests: the caller cannot choose the credential owner."""
import unittest

from llm_gateway.nsdp_query import QueryError
from llm_gateway.nsdp_tool_server import handle


class Query:
    def __init__(self):
        self.calls = []

    def releases(self, owner, release):
        self.calls.append((owner, release))
        if release == 'missing':
            raise QueryError('authorization_required')
        return 'synthetic G4'


class ToolServerTests(unittest.TestCase):
    def test_lookup_binds_owner_outside_model_arguments(self):
        query = Query()
        msg = {'id': 1, 'method': 'tools/call', 'params': {
            'name': 'lookup_release', 'arguments': {'release': 'BL1.5.0'}}}
        response = handle(msg, owner='ou_a', query=query)
        self.assertFalse(response['result']['isError'])
        self.assertEqual(query.calls, [('ou_a', 'BL1.5.0')])
        msg['params']['arguments']['owner'] = 'ou_b'
        self.assertIn('error', handle(msg, owner='ou_a', query=query))
        self.assertEqual(len(query.calls), 1)

    def test_authorization_error_is_structured(self):
        msg = {'id': 1, 'method': 'tools/call', 'params': {
            'name': 'lookup_release', 'arguments': {'release': 'missing'}}}
        result = handle(msg, owner='ou_a', query=Query())['result']
        self.assertTrue(result['isError'])
        self.assertEqual(result['content'][0]['text'], 'NSDP query failed: authorization_required')

    def test_tool_duration_is_recorded_with_name_and_status(self):
        recorded = []
        msg = {'id': 1, 'method': 'tools/call', 'params': {
            'name': 'lookup_release', 'arguments': {'release': 'BL1.5.0'}}}
        handle(msg, owner='ou_a', query=Query(),
               record=lambda tool, status, elapsed: recorded.append((tool, status, elapsed)))
        self.assertEqual(recorded[0][0], 'meeting_nsdp_lookup_release')
        self.assertEqual(recorded[0][1], 'ok')
        self.assertGreaterEqual(recorded[0][2], 0)
        failing = {'id': 2, 'method': 'tools/call', 'params': {
            'name': 'lookup_release', 'arguments': {'release': 'missing'}}}
        handle(failing, owner='ou_a', query=Query(),
               record=lambda tool, status, elapsed: recorded.append((tool, status, elapsed)))
        self.assertEqual(recorded[1][:2], ('meeting_nsdp_lookup_release', 'error'))


if __name__ == '__main__':
    unittest.main()
