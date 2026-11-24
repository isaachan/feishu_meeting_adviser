"""Fallback policy for development and CI (non-macOS, non-Windows)."""
from pathlib import Path


def create_sources(mic_enabled: bool) -> list:
    from recorder.audio.sck_capture import SwiftLoopbackSource
    from recorder.audio.sources import MicSource

    sources: list = [SwiftLoopbackSource('loopback')]
    if mic_enabled:
        sources.append(MicSource('mic'))
    return sources


def capture_root() -> Path:
    return Path.home() / '.local' / 'state' / 'gui' / 'recorder' / 'audio'


def close_while_capturing() -> str:
    return 'hide'
