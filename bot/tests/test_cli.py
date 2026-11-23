"""CLI defaults: rich-text rendering is on unless explicitly disabled."""
import unittest
from unittest.mock import patch

from bot.__main__ import _env_flag


class RichTextDefaultTests(unittest.TestCase):
    def test_default_is_on(self):
        with patch.dict('os.environ', {}, clear=True):
            self.assertTrue(_env_flag('BOT_RICH_TEXT'))

    def test_env_can_disable(self):
        for value in ('0', 'false', 'off', 'no'):
            with self.subTest(value=value):
                with patch.dict('os.environ', {'BOT_RICH_TEXT': value}):
                    self.assertFalse(_env_flag('BOT_RICH_TEXT'))


if __name__ == '__main__':
    unittest.main()
