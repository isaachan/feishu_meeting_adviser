"""PyInstaller entry point for the shared Qt client on Windows."""
import os
import sys

# PyInstaller's windowed bootloader supplies no standard streams. Recorder
# diagnostics use print(), so provide streams without requiring a console.
if sys.stdout is None:
    sys.stdout = open(os.devnull, 'w', encoding='utf-8')
if sys.stderr is None:
    sys.stderr = open(os.devnull, 'w', encoding='utf-8')

from macos_gui.__main__ import main

if __name__ == '__main__':
    sys.exit(main())
