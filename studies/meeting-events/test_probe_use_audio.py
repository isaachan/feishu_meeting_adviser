import unittest
from unittest.mock import patch

from probe_use_audio import assess, details, observation


class UseAudioProbeTest(unittest.TestCase):
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
