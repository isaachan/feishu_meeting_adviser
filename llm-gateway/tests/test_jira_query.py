import json
import subprocess
import unittest
from types import SimpleNamespace

from llm_gateway.jira_tool_server import JiraQuery, handle
from llm_gateway.nsdp_query import QueryError


class JiraQueryTests(unittest.TestCase):
    def query(self, run):
        return JiraQuery(lambda owner: {'ou_A': 'secret-A', 'ou_B': 'secret-B'}[owner],
                         url='https://jira.example', noam_bin='/noam', run=run)

    def test_owner_and_read_only_isolated_query(self):
        seen = []
        def run(argv, **kw):
            seen.append((argv, kw['env']))
            return SimpleNamespace(returncode=0, stdout='{"issues":[]}', stderr='')
        query = self.query(run)
        query.execute('ou_A', 'search_issues', {'jql': 'project = "ABC"', 'start': 50})
        query.execute('ou_B', 'get_issue', {'key': 'ABC-123'})
        self.assertEqual(seen[0][0], ['/noam', 'jira', 'issue', 'search',
                                     'project = "ABC"', '--max', '50', '--start', '50'])
        self.assertEqual(seen[0][1]['JIRA_TOKEN'], 'secret-A')
        self.assertEqual(seen[1][1]['JIRA_TOKEN'], 'secret-B')
        self.assertEqual(seen[0][1]['JIRA_READ_ONLY'], 'true')
        self.assertNotEqual(seen[0][1]['HOME'], seen[1][1]['HOME'])

    def test_rejects_arbitrary_commands_and_invalid_input(self):
        query = self.query(lambda *a, **kw: self.fail('must not run'))
        for name, args in [('edit', {}), ('get_issue', {'key': '--help'}),
                           ('search_issues', {'jql': 'x', 'start': -1}),
                           ('projects', {'url': 'https://evil'})]:
            with self.assertRaises(QueryError):
                query.execute('ou_A', name, args)

    def test_distinguishes_auth_permission_and_timeout(self):
        for status, expected in [('401', 'authorization_required'), ('403', 'forbidden'),
                                 ('500', 'unavailable')]:
            query = self.query(lambda *a, **kw: SimpleNamespace(returncode=1, stdout='', stderr=status))
            with self.assertRaises(QueryError) as caught:
                query.execute('ou_A', 'projects', {})
            self.assertEqual(caught.exception.code, expected)
        def timeout(*a, **kw):
            raise subprocess.TimeoutExpired('noam', 1)
        with self.assertRaises(QueryError) as caught:
            self.query(timeout).execute('ou_A', 'projects', {})
        self.assertEqual(caught.exception.code, 'timeout')

    def test_mcp_reports_missing_auth_without_token(self):
        def missing(owner):
            raise QueryError('authorization_required')
        query = JiraQuery(missing, url='https://jira.example')
        response = handle({'id': 1, 'method': 'tools/call',
                           'params': {'name': 'get_issue', 'arguments': {'key': 'ABC-1'}}},
                          owner='ou_A', query=query)
        self.assertTrue(response['result']['isError'])
        self.assertIn('authorization_required', json.dumps(response))


if __name__ == '__main__':
    unittest.main()
