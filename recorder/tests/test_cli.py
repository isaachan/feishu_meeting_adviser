"""Plan-2.3 acceptance: CLI contract — argument validation, replay pipeline end
to end against a fake ingest server, exit codes (appendix A.5)."""
import json
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from recorder.__main__ import build_parser, validate_args, EXIT_ARGS, EXIT_INTERRUPT

sys.path.insert(0, str(Path(__file__).resolve().parents[0]))
from test_uplink import FakeIngest  # noqa: E402


class ValidateArgsTests(unittest.TestCase):
    def args(self, **overrides):
        base = {'session': 's-abc123', 'event': 'cal_x/evt_y',
                'url': 'ws://127.0.0.1:8400/ingest',
                'token': 'st-abcdefghijklmnop123456', 'duration': None}
        base.update(overrides)
        return type('Args', (), base)

    def test_valid(self):
        self.assertIsNone(validate_args(self.args()))

    def test_invalid_shapes(self):
        cases = [
            {'session': 'local:abc'}, {'session': 's'}, {'session': 's/x'},
            {'event': 'no-slash'}, {'event': '/evt'},
            {'url': 'wss://127.0.0.1:8400/ingest'}, {'url': 'ws://host/other'},
            {'token': 'short'}, {'token': "st-ok'; rm -rf /"},
            {'duration': 0}, {'duration': -5},
        ]
        for overrides in cases:
            with self.subTest(overrides):
                self.assertIsNotNone(validate_args(self.args(**overrides)))

    def test_user_access_token_never_a_valid_flag(self):
        # structural red line: the CLI has no flag that could carry credentials
        parser = build_parser()
        flags = {action.dest for action in parser._actions}
        for sub in [action for action in parser._actions if hasattr(action, 'choices')]:
            for subparser in (sub.choices or {}).values():
                flags |= {a.dest for a in subparser._actions}
        self.assertNotIn('user_access_token', flags)
        self.assertNotIn('access_token', flags)


class ReplayCliTests(unittest.TestCase):
    def run_replay(self, server, script_rows, extra=(), timeout=60):
        with TemporaryDirectory() as tmp:
            script = Path(tmp) / 'script.jsonl'
            script.write_text('\n'.join(json.dumps(row) for row in script_rows))
            proc = subprocess.Popen(
                [sys.executable, '-m', 'recorder', 'replay', str(script),
                 '--session', 's-testcli1', '--event', 'cal_x/evt_y',
                 '--url', server.url, '--token', 'st-goodtoken0123456789',
                 '--data-root', tmp, *extra],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and proc.poll() is None:
                time.sleep(0.1)
            if proc.poll() is None:
                proc.kill()
            out = proc.communicate()[0].decode()
            return proc.returncode, out

    def test_replay_streams_and_exits_zero(self):
        server = FakeIngest().start()
        self.addCleanup(server.stop)
        code, out = self.run_replay(server, [
            {'delay_ms': 50}, {'delay_ms': 50}, {'delay_ms': 50}])
        self.assertEqual(code, 0, out)
        self.assertGreaterEqual(len(server.frames.get('loopback', [])), 3)
        self.assertIn('loopback', server.stream_ends)
        self.assertIn('[summary]', out)
        self.assertIn('[exit] loopback: code=0', out)

    def test_bad_token_exits_two(self):
        server = FakeIngest().start()
        self.addCleanup(server.stop)
        server.bad_tokens.add('st-bad00000000000000')
        with TemporaryDirectory() as tmp:
            script = Path(tmp) / 'script.jsonl'
            script.write_text(json.dumps({'delay_ms': 50}))
            proc = subprocess.Popen(
                [sys.executable, '-m', 'recorder', 'replay', str(script),
                 '--session', 's-testcli1', '--event', 'cal_x/evt_y',
                 '--url', server.url, '--token', 'st-bad00000000000000',
                 '--data-root', tmp],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            out = proc.communicate(timeout=30)[0].decode()
            self.assertEqual(proc.returncode, 2, out)
            self.assertIn('bad_token', out)

    def test_invalid_args_exit_two(self):
        proc = subprocess.Popen(
            [sys.executable, '-m', 'recorder', 'replay', 'whatever.jsonl',
             '--session', 'local:abc', '--event', 'cal_x/evt_y',
             '--url', 'ws://127.0.0.1:8400/ingest', '--token', 'st-abcdefghijklmnop'],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        out = proc.communicate(timeout=15)[0].decode()
        self.assertEqual(proc.returncode, 2, out)
        self.assertIn('--session', out)


if __name__ == '__main__':
    unittest.main()
