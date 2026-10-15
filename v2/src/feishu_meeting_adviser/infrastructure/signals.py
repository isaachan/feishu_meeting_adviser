"""Scoped process-signal handling for stoppable application services."""
from contextlib import contextmanager
import signal
import threading


@contextmanager
def stop_on_signals(stop):
    previous = {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, lambda *_: stop())
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
