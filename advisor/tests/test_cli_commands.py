from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
import unittest

from feishu_meeting_adviser.cli.commands import COMMAND_HANDLERS, handle_replies


class CLICommandTests(unittest.TestCase):
    def test_every_parser_command_has_an_independent_handler(self):
        self.assertEqual(set(COMMAND_HANDLERS), {'status', 'consume', 'replies', 'resolve-reply'})
        self.assertEqual(len(set(COMMAND_HANDLERS.values())), len(COMMAND_HANDLERS))

    def test_handler_can_be_called_without_cli_bootstrap(self):
        with tempfile.TemporaryDirectory() as directory:
            context = SimpleNamespace(
                manager=SimpleNamespace(saved_meeting=lambda: {'meeting_id': 'synthetic-meeting'}),
                config=SimpleNamespace(data_directory=Path(directory)),
            )
            output = StringIO()
            with redirect_stdout(output):
                result = handle_replies(context, SimpleNamespace(show_text=False))
            self.assertEqual(result, 0)
            self.assertEqual(json.loads(output.getvalue()), [])
