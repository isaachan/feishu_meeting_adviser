import contextlib
import io
import json
import unittest
from unittest.mock import patch

from probe_use_audio import assess, details, fetch_full, main, observation, resolve_active_meeting


class UseAudioProbeTest(unittest.TestCase):
    def test_resolve_uses_user_active_meeting(self):
        with patch('probe_use_audio.active_ids', return_value=['m1', 'm1']) as active, \
                patch('probe_use_audio.details', return_value={
                    'code': 0, 'data': {'meeting': {
                        'meeting_no': '123456789', 'status': 2}}}) as get_details:
            meeting_id, result = resolve_active_meeting('123456789', 'user-token')
            self.assertEqual(meeting_id, 'm1')
            self.assertEqual(result['code'], 0)
        active.assert_called_once_with('123456789', 'user-token')
        get_details.assert_called_once_with('m1', 'user-token')

    def test_once_prints_complete_response_without_prompting(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict('os.environ', {'BOT_APP_ID': 'app', 'BOT_APP_SECRET': 'secret'},
                        clear=True), \
                patch('sys.argv', ['probe_use_audio.py', '123456789', '--once']), \
                patch('probe_use_audio.authorize', return_value='user-token') as authorize, \
                patch('probe_use_audio.active_ids', return_value=['meeting-id']), \
                patch('probe_use_audio.details', return_value={
                    'code': 0, 'msg': 'success', 'request_id': 'abc',
                    'data': {'meeting': {'meeting_no': '123456789', 'status': 2,
                                        'participants': [{'id': 'host', 'name': 'Kai'}],
                                        'extra_field': {'nested': 42}}}}), \
                patch('builtins.input', side_effect=AssertionError('must not prompt')):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(main(), 0)
        authorize.assert_called_once_with('app', 'secret', 8767)
        record = json.loads(stdout.getvalue())
        self.assertEqual(record['response']['request_id'], 'abc')
        self.assertEqual(record['response']['data']['meeting']['participants'][0]['name'], 'Kai')
        self.assertEqual(record['response']['data']['meeting']['extra_field'], {'nested': 42})

    def test_dump_prints_raw_active_meeting_response(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        active = {'code': 0, 'msg': 'success', 'data': {'meetings': [
            {'meeting_id': 'meeting-id', 'meeting_no': '123456789',
             'extra_field': {'nested': 7}}]}}
        detail = {'code': 0, 'data': {'meeting': {
            'meeting_no': '123456789', 'status': 2}}}

        def fetch(url, token):
            return active if 'user_active_meeting' in url else detail

        with patch.dict('os.environ', {'BOT_APP_ID': 'app', 'BOT_APP_SECRET': 'secret'},
                        clear=True), \
                patch('sys.argv', ['probe_use_audio.py', '123456789', '--once', '--dump']), \
                patch('probe_use_audio.authorize', return_value='user-token'), \
                patch('probe_use_audio.fetch_full', side_effect=fetch), \
                patch('probe_use_audio.active_ids', return_value=['meeting-id']), \
                patch('probe_use_audio.details', return_value=detail):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(main(), 0)
        records = [json.loads(line) for line in stdout.getvalue().splitlines()]
        self.assertEqual([row['phase'] for row in records],
                         ['user_active_meeting', 'preflight'])
        self.assertEqual(records[0]['response']['data']['meetings'][0]['extra_field'],
                         {'nested': 7})

    def test_details_requests_ability_and_participants(self):
        result = {'code': 0, 'msg': 'success', 'data': {'meeting': {'status': 2}}}
        with patch('probe_use_audio.fetch_full', return_value=result) as fetch:
            self.assertEqual(details('a/b', 'token'), result)
        url = fetch.call_args.args[0]
        self.assertIn('/vc/v1/meetings/a%2Fb?', url)
        self.assertIn('with_meeting_ability=true', url)
        self.assertIn('with_participants=true', url)

    def test_fetch_full_keeps_top_level_fields(self):
        response = io.BytesIO(b'{"code":0,"msg":"ok","extra":{"x":1},"data":{}}')
        response.status = 200
        with patch('probe_use_audio.request.urlopen', return_value=response):
            result = fetch_full('https://open.feishu.cn/open-apis/vc/v1/meetings/m1', 'token')
        self.assertEqual(result['extra'], {'x': 1})

    def test_observation_keeps_missing_distinct_from_false(self):
        self.assertFalse(observation({'ability': {}})['use_audio_present'])
        row = observation({'ability': {'use_audio': False}, 'participants': [
            {'id': 'host', 'status': 2, 'mic_state': 'muted'}]})
        self.assertTrue(row['use_audio_present'])
        self.assertIs(row['use_audio'], False)

    def test_assessment_requires_stable_boolean_samples(self):
        row = lambda value: {'meeting_status': 2, 'use_audio_present': True,
                             'use_audio': value}
        self.assertIn('未随', assess([[row(True)], [row(True)], [row(True)]]))
        self.assertIn('会议级', assess([[row(False)], [row(True)], [row(False)]]))
        self.assertIn('无法判断', assess([[row(True), row(False)], [row(True)], [row(False)]]))
        self.assertIn('无法判断', assess([[{'meeting_status': 2, 'use_audio_present': False,
                                       'use_audio': None}],
                                      [row(True)], [row(False)]]))
        ended = {**row(False), 'meeting_status': 3}
        self.assertIn('无法判断', assess([[row(False)], [row(True)], [ended]]))


if __name__ == '__main__':
    unittest.main()
