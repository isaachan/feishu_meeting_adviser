"""macOS policy: SCK loopback, mute-gated Feishu mic, Application Support."""
from pathlib import Path


def create_sources(mic_enabled: bool) -> list:
    from recorder.audio.macos_mic import MacFeishuMicSource
    from recorder.audio.sck_capture import SwiftLoopbackSource

    sources: list = [SwiftLoopbackSource('loopback')]
    if mic_enabled:
        sources.append(MacFeishuMicSource('mic'))
    return sources


def capture_root() -> Path:
    return Path.home() / 'Library' / 'Application Support' / 'gui' / 'recorder' / 'audio'


def close_while_capturing() -> str:
    return 'hide'
