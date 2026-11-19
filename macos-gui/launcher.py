"""PyInstaller entry point for the Qt client on macOS."""
import os
import sys
from pathlib import Path

# PyInstaller's windowed bootloader supplies no standard streams. Recorder
# diagnostics use print(), so provide streams without requiring a console.
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w', encoding='utf-8')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w', encoding='utf-8')


def _use_bundled_helpers():
    """Point recorder at the precompiled Swift helpers inside the bundle.

    The build compiles sck_capture.swift / macos_mute.swift with swiftc into
    the bundle (DESIGN §7): target Macs need no Xcode Command Line Tools.
    """
    if not getattr(sys, 'frozen', False) or sys.platform != 'darwin':
        return
    base = Path(getattr(sys, '_MEIPASS', ''))
    for name, env in (('sck_capture', 'LOCAL_MEETING_RECORDER_SCK_BIN'),
                      ('macos_mute', 'LOCAL_MEETING_RECORDER_MUTE_BIN')):
        binary = base / name
        if binary.is_file():
            os.environ.setdefault(env, str(binary))


_use_bundled_helpers()

from macos_gui.__main__ import main

if __name__ == '__main__':
    sys.exit(main())
