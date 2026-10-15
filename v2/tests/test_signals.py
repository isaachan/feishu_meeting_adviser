import signal
import unittest

from feishu_meeting_adviser.infrastructure.signals import stop_on_signals


class SignalContextTests(unittest.TestCase):
    def test_installs_callback_and_restores_previous_handlers(self):
        previous = {signum: signal.getsignal(signum)
                    for signum in (signal.SIGINT, signal.SIGTERM)}
        stopped = []

        with stop_on_signals(lambda: stopped.append(True)):
            handler = signal.getsignal(signal.SIGTERM)
            self.assertTrue(callable(handler))
            handler(signal.SIGTERM, None)
            self.assertEqual(stopped, [True])

        self.assertEqual(
            {signum: signal.getsignal(signum) for signum in (signal.SIGINT, signal.SIGTERM)},
            previous,
        )


if __name__ == '__main__':
    unittest.main()
