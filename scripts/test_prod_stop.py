"""Production stop must never silently interrupt active sessions."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('prod_stop', Path(__file__).with_name('prod-stop.py'))
stop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stop)


class StopTests(unittest.TestCase):
    @patch.object(stop.subprocess, 'run')
    @patch.object(stop, 'api')
    @patch('sys.argv', ['prod-stop'])
    def test_refuses_active_session(self, api, run):
        run.return_value.returncode = 0
        api.return_value = {'sessions': [{'session_id': 's-1', 'phase': 'recording'}]}
        with self.assertRaisesRegex(RuntimeError, 'active/draining'):
            stop.main()
        self.assertEqual(run.call_count, 1)

    @patch.object(stop, 'systemctl')
    @patch.object(stop.subprocess, 'run')
    @patch.object(stop, 'api')
    @patch.object(stop.time, 'sleep')
    @patch('sys.argv', ['prod-stop', '--drain'])
    def test_drains_before_stopping(self, sleep, api, run, ctl):
        run.return_value.returncode = 0
        api.side_effect = [
            {'sessions': [{'session_id': 's-1', 'phase': 'recording'}]},
            {'phase': 'draining'}, {'sessions': []}, {'sessions': []},
        ]
        stop.main()
        self.assertEqual(api.call_args_list[1].args, ('/admin/sessions/s-1/end', 'POST'))
        self.assertEqual(ctl.call_args_list[0].args, ('stop', 'meeting-advisor@bot.service'))
        self.assertEqual(ctl.call_args_list[-1].args, ('stop', 'meeting-advisor@auth.service'))

    @patch.object(stop, 'systemctl')
    @patch.object(stop.subprocess, 'run')
    @patch.object(stop, 'api')
    @patch('sys.argv', ['prod-stop', '--drain', '--timeout', '0'])
    def test_timeout_leaves_bot_running(self, api, run, ctl):
        run.return_value.returncode = 0
        sessions = {'sessions': [{'session_id': 's-1', 'phase': 'draining'}]}
        api.side_effect = [sessions, {'phase': 'draining'}, sessions]
        with self.assertRaisesRegex(RuntimeError, 'timed out'):
            stop.main()
        ctl.assert_not_called()


if __name__ == '__main__':
    unittest.main()
