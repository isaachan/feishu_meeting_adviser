from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace
import json
import unittest

from feishu_meeting_adviser.cli.commands import COMMAND_HANDLERS, handle_conflicts


class _Manager:
    def saved_meeting(self):
        return {'meeting_id': 'synthetic-meeting'}


class _Store:
    def __init__(self):
        self.calls = []

    def list_conflicts(self, meeting_id, *, show_text):
        self.calls.append((meeting_id, show_text))
        return [{'delivery_id': 'synthetic-delivery'}]


class CLICommandTests(unittest.TestCase):
    def test_every_parser_command_has_an_independent_handler(self):
        self.assertEqual(set(COMMAND_HANDLERS), {
            'join', 'leave', 'collect', 'status', 'consume', 'reset-session',
            'resolve-conflict', 'prune', 'delete-meeting-data', 'conflicts',
            'replies', 'resolve-reply',
        })
        self.assertEqual(len(set(COMMAND_HANDLERS.values())), len(COMMAND_HANDLERS))

    def test_handler_can_be_called_without_cli_bootstrap(self):
        store = _Store()
        context = SimpleNamespace(manager=_Manager(), store=store)
        output = StringIO()

        with redirect_stdout(output):
            result = handle_conflicts(context, SimpleNamespace(show_text=False))

        self.assertEqual(result, 0)
        self.assertEqual(store.calls, [('synthetic-meeting', False)])
        self.assertEqual(json.loads(output.getvalue()), [
            {'delivery_id': 'synthetic-delivery'},
        ])


if __name__ == '__main__':
    unittest.main()
