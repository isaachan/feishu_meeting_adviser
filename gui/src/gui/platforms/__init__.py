"""Platform-specific policy: capture sources, retention root, close behavior.

Platform modules must keep recorder imports inside their functions: CI
imports this package on Linux, where the Windows/macOS backends are absent.
"""
import sys
from pathlib import Path


def _impl():
    if sys.platform == 'win32':
        from . import windows
        return windows
    if sys.platform == 'darwin':
        from . import macos
        return macos
    from . import generic
    return generic


def create_sources(mic_enabled: bool) -> list:
    return _impl().create_sources(mic_enabled)


def capture_root() -> Path:
    return _impl().capture_root()


def close_while_capturing() -> str:
    """'hide' keeps capturing with the window hidden; 'block' refuses to close."""
    return _impl().close_while_capturing()
