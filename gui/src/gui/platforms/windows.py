"""Windows policy: process loopback, default mic, LocalAppData, block close."""
import os
from pathlib import Path


def create_sources(mic_enabled: bool) -> list:
    from recorder.audio.windows_loopback import WindowsLoopbackSource
    from recorder.audio.windows_mic import WindowsMicSource

    sources: list = [WindowsLoopbackSource()]
    if mic_enabled:
        sources.append(WindowsMicSource())
    return sources


def capture_root() -> Path:
    base = os.environ.get('LOCALAPPDATA')
    root = Path(base) if base else Path.home() / 'AppData' / 'Local'
    return root / 'gui' / 'recorder' / 'audio'


def close_while_capturing() -> str:
    return 'block'
