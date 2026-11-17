import contextlib
import io
import unittest
from unittest.mock import patch

from probe_use_audio import assess, details, main, observation, resolve_active_meeting


class UseAudioProbeTest(unittest.TestCase):
    def test_resolve_uses_user_active_meeting(self):
        with patch('probe_use_audio.active_ids', return_value=['m1', 'm1']) as active, \
                patch('probe_use_audio.details', return_value={
                    'meeting_no': '123456789', 'status': 2}) as get_details:
            self.assertEqual(resolve_active_meeting('123456789', 'user-token'), 'm1')
        active.assert_called_once_with('123456789', 'user-token')
        get_details.assert_called_once_with('m1', 'user-token')

    def test_oauth_preflight_reports_missing_ability_without_prompting(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict('os.environ', {'BOT_APP_ID': 'app', 'BOT_APP_SECRET': 'secret'},
                        clear=True), \
                patch('sys.argv', ['probe_use_audio.py', '123456789']), \
                patch('probe_use_audio.authorize', return_value='user-token') as authorize, \
                patch('probe_use_audio.active_ids', return_value=['meeting-id']), \
                patch('probe_use_audio.details', return_value={
                    'meeting_no': '123456789', 'status': 2, 'participants': []}), \
                patch('builtins.input', side_effect=AssertionError('must not prompt')):
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                self.assertEqual(main(), 0)
        authorize.assert_called_once_with('app', 'secret', 8767)
        self.assertIn('"use_audio_present": false', stdout.getvalue())
        self.assertIn('用户身份的会议详情响应未返回', stderr.getvalue())

    def test_details_requests_ability_and_participants(self):
        with patch('probe_use_audio.fetch', return_value={'meeting': {'status': 2}}) as fetch:
            self.assertEqual(details('a/b', 'token'), {'status': 2})
        url = fetch.call_args.args[0]
        self.assertIn('/vc/v1/meetings/a%2Fb?', url)
        self.assertIn('with_meeting_ability=true', url)
        self.assertIn('with_participants=true', url)

    def test_observation_keeps_missing_distinct_from_false(self):
        self.assertFalse(observation({'ability': {}})['use_audio_present'])
        row = observation({'ability': {'use_audio': False}, 'participants': [
            {'id': 'host', 'status': 2, 'mic_state': 'muted'}]})
        self.assertTrue(row['use_audio_present'])
        self.assertIs(row['use_audio'], False)
        self.assertEqual(row['participants'][0]['audio_or_host_fields'],
                         {'mic_state': 'muted'})

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
