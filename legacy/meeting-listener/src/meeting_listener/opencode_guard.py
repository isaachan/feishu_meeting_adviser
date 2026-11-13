"""Stop an owned HTTP server on consumer EOF/signals, including parent SIGKILL."""
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import threading

from .infrastructure.file_lock import FileLock
from .storage import private_directory


def main():
    runtime = Path(sys.argv[1])
    if sys.argv[2] != '--' or not runtime.name.startswith('run-'):
        return 2
    os.umask(0o077)
    private_directory(runtime)
    stopping = threading.Event()
    for signum in (signal.SIGINT,signal.SIGTERM):
        signal.signal(signum,lambda *_:stopping.set())
    def parent_eof():
        while sys.stdin.buffer.read(1):
            pass
        stopping.set()
    threading.Thread(target=parent_eof,daemon=True).start()
    process = None
    try:
        with FileLock(runtime/'guard.lock'):
            process = subprocess.Popen(sys.argv[3:],stdin=subprocess.DEVNULL)
            while process.poll() is None and not stopping.wait(0.05):
                pass
            if process.poll() is None:
                os.killpg(os.getpgrp(),signal.SIGTERM)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    os.killpg(os.getpgrp(),signal.SIGKILL)
            return process.wait()
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()
        shutil.rmtree(runtime)


if __name__ == '__main__':
    raise SystemExit(main())
