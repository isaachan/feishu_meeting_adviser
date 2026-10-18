from pathlib import Path
import unittest

from feishu_meeting_adviser.config import (
    ConfigDocument,
    ConfigError,
    HTTPSettings,
    MeetingContextSettings,
    RuntimeSettings,
    StorageSettings,
    _parse_llm,
)


class ConfigSectionTests(unittest.TestCase):
    def test_reply_evidence_window_has_independent_default_and_override(self):
        default = _parse_llm(ConfigDocument({}).section('llm'), Path('/tmp'))
        self.assertEqual((default.freshness_seconds, default.reply_evidence_seconds), (120, 600))
        custom = _parse_llm(ConfigDocument({'llm': {
            'freshness_seconds': 90, 'reply_evidence_seconds': 900,
        }}).section('llm'), Path('/tmp'))
        self.assertEqual((custom.freshness_seconds, custom.reply_evidence_seconds), (90, 900))
        with self.assertRaisesRegex(ConfigError, 'reply_evidence_seconds'):
            _parse_llm(ConfigDocument({'llm': {'freshness_seconds': 900}}).section('llm'),
                       Path('/tmp'))

    def test_storage_http_and_runtime_sections_parse_independently(self):
        document = ConfigDocument({
            'storage': {'directory': 'state', 'retention_days': 9},
            'http': {'request_seconds': 1.5, 'timeout_seconds': 12},
            'runtime': {'poll_seconds': 2, 'cooldown_seconds': 40},
        })

        storage = StorageSettings.parse(document.section('storage'), Path('/tmp/config'))
        http = HTTPSettings.parse(document.section('http'))
        runtime = RuntimeSettings.parse(document.section('runtime'))

        self.assertEqual(storage.directory, Path('/tmp/config/state'))
        self.assertEqual(storage.retention_days, 9)
        self.assertEqual(http.timeout_seconds, 12)
        self.assertEqual(http.request_seconds, 1.5)
        self.assertEqual(runtime, RuntimeSettings(2, 40))

    def test_meeting_context_owns_binding_validation(self):
        valid = ConfigDocument({'meeting_context': {
            'calendar_id': 'calendar',
            'identity': 'application',
        }})
        settings = MeetingContextSettings.parse(valid.section('meeting_context'))
        self.assertTrue(settings.enabled)
        self.assertEqual(settings.identity, 'application')
        discovered = MeetingContextSettings.parse(ConfigDocument({
            'meeting_context': {'calendar_id': 'calendar'}
        }).section('meeting_context'))
        self.assertTrue(discovered.enabled)

        for values in (
            {'event_id': 'event_0'},
            {'calendar_id': 'calendar', 'event_id': 'event_0'},
            {'calendar_id': '../calendar'},
            {'identity': 'other'},
            {'unknown': True},
        ):
            with self.subTest(values=values), self.assertRaises(ConfigError):
                MeetingContextSettings.parse(
                    ConfigDocument({'meeting_context': values}).section('meeting_context'))


if __name__ == '__main__':
    unittest.main()
