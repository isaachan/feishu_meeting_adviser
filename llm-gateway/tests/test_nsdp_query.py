"""Tests for the credential boundary of the read-only NSDP query."""
import subprocess
import json
import unittest
from types import SimpleNamespace

from llm_gateway.nsdp_query import NsdpQuery, QueryError


class NsdpQueryTests(unittest.TestCase):
    def test_query_uses_owner_credential_only_in_child_environment(self):
        calls = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs))
            return SimpleNamespace(returncode=0, stdout='BL1.5.0 G4 2026-12-01', stderr='')

        query = NsdpQuery(lambda owner: {'ou_a': 'secret-token'}[owner], run=run)
        self.assertIn('G4', query.releases('ou_a', 'BL1.5.0'))
        argv, kwargs = calls[0]
        self.assertEqual(argv, ['noam', 'nsdp', 'releases', '--release', 'BL1.5.0'])
        self.assertEqual(kwargs['env']['NSDP_TOKEN'], 'secret-token')
        self.assertNotIn('secret-token', argv)
        self.assertNotEqual(kwargs['env']['HOME'], __import__('os').path.expanduser('~'))

    def test_invalid_release_is_rejected_before_fetching_credentials(self):
        query = NsdpQuery(lambda _: self.fail('unexpected credential lookup'))
        with self.assertRaises(QueryError) as caught:
            query.releases('ou_a', '--help')
        self.assertEqual(caught.exception.code, 'invalid_release')

    def test_errors_do_not_expose_process_output_or_token(self):
        def run(*args, **kwargs):
            return SimpleNamespace(returncode=1, stdout='', stderr='401 secret-token')

        with self.assertRaises(QueryError) as caught:
            NsdpQuery(lambda _: 'secret-token', run=run).releases('ou_a', 'BL1.5.0')
        self.assertEqual(str(caught.exception), 'authorization_required')

    def test_timeout_is_reported_without_process_details(self):
        def run(*args, **kwargs):
            raise subprocess.TimeoutExpired('private', 1)

        with self.assertRaises(QueryError) as caught:
            NsdpQuery(lambda _: 'secret-token', run=run).releases('ou_a', 'BL1.5.0')
        self.assertEqual(str(caught.exception), 'timeout')

    def test_info_returns_valid_catalog_using_owner_credentials(self):
        catalog = {'tenants': ['NIO'], 'platforms': [{'versions': ['BL1.7.0']}]}
        calls = []

        def run(argv, **kwargs):
            calls.append((argv, kwargs['env']['NSDP_TOKEN']))
            return SimpleNamespace(returncode=0, stdout=json.dumps(catalog), stderr='')

        result = NsdpQuery(lambda owner: {'ou_a': 'synthetic-token'}[owner], run=run).info('ou_a')
        self.assertEqual(result, catalog)
        self.assertEqual(calls, [(['noam', 'nsdp', 'info'], 'synthetic-token')])

    def test_info_rejects_invalid_or_oversized_catalog(self):
        for stdout, code in [('{', 'output_invalid'),
                             (json.dumps({'tenants': [], 'platforms': [1]}), 'output_invalid'),
                             ('x' * (2 * 1024 * 1024 + 1), 'output_too_large')]:
            with self.subTest(code=code):
                def run(*args, **kwargs):
                    return SimpleNamespace(returncode=0, stdout=stdout, stderr='')
                with self.assertRaises(QueryError) as caught:
                    NsdpQuery(lambda _: 'synthetic-token', run=run).info('ou_a')
                self.assertEqual(caught.exception.code, code)


if __name__ == '__main__':
    unittest.main()
