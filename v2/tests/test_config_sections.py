from pathlib import Path
import unittest

from feishu_meeting_adviser.config import (
    ConfigDocument,
    ConfigError,
    HTTPSettings,
    MeetingContextSettings,
    RuntimeSettings,
    StorageSettings,
)


class ConfigSectionTests(unittest.TestCase):
    def test_storage_http_and_runtime_sections_parse_independently(self):
        document = ConfigDocument({
            'storage': {'directory': 'state', 'retention_days': 9},
            'http': {'request_seconds': 1.5},
            'probe': {'timeout_seconds': 12},
            'runtime': {'poll_seconds': 2, 'cooldown_seconds': 40},
        })

        storage = StorageSettings.parse(document.section('storage'), Path('/tmp/config'))
        http = HTTPSettings.parse(document.section('http'), document.section('probe'))
        runtime = RuntimeSettings.parse(document.section('runtime'))

        self.assertEqual(storage.directory, Path('/tmp/config/state'))
        self.assertEqual(storage.retention_days, 9)
        self.assertEqual(http.timeout_seconds, 12)
        self.assertEqual(http.request_seconds, 1.5)
        self.assertEqual(runtime, RuntimeSettings(2, 40))

    def test_meeting_context_owns_binding_validation(self):
        valid = ConfigDocument({'meeting_context': {
            'calendar_id': 'calendar',
            'event_id': 'event_0',
            'identity': 'application',
        }})
        settings = MeetingContextSettings.parse(valid.section('meeting_context'))
        self.assertTrue(settings.enabled)
        self.assertEqual(settings.identity, 'application')

        for values in (
            {'calendar_id': 'calendar'},
            {'calendar_id': 'calendar', 'event_id': '../event'},
            {'identity': 'other'},
            {'unknown': True},
        ):
            with self.subTest(values=values), self.assertRaises(ConfigError):
                MeetingContextSettings.parse(
                    ConfigDocument({'meeting_context': values}).section('meeting_context'))


if __name__ == '__main__':
    unittest.main()
