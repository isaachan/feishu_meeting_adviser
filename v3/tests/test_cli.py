import json
import tempfile
import unittest
from pathlib import Path

from feishu_spectator.__main__ import COMMAND_HANDLERS, main, parser
from feishu_spectator.config import load_config


def write_config(directory: Path) -> Path:
    path = directory / 'config.yaml'
    path.write_text('app:\n  id: "cli_test"\n  secret: "secret_test"\n')
    path.chmod(0o600)
    return path


class ParserTests(unittest.TestCase):
    def test_supported_commands(self):
        self.assertEqual(set(COMMAND_HANDLERS),
                         {'login', 'oauth-status', 'logout', 'meetings', 'listen',
                          'status', 'recent'})

    def test_no_command_shows_help(self):
        self.assertEqual(main([]), 0)

    def test_invalid_command_rejected(self):
        with self.assertRaises(SystemExit):
            parser().parse_args(['fly'])


class HandlerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = write_config(Path(self.tmp.name))
        self.config = load_config(self.path)
        self.context = {'config': self.config, 'stop': lambda: False,
                        'diagnose': lambda record: None}

    def tearDown(self):
        self.tmp.cleanup()

    def test_oauth_status_before_login(self):
        args = parser().parse_args(['--config', str(self.path), 'oauth-status'])
        self.assertEqual(COMMAND_HANDLERS['oauth-status'](self.context, args), 0)

    def test_logout_without_credentials(self):
        args = parser().parse_args(['--config', str(self.path), 'logout'])
        self.assertEqual(COMMAND_HANDLERS['logout'](self.context, args), 0)


if __name__ == '__main__':
    unittest.main()
