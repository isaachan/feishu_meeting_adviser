"""5.2 dual-path audio capture: frames, sources, session."""
from .frames import AudioFrame, SourceEvent
from .session import CaptureSession
from .sources import AudioSource, FakeSource, MicSource

__all__ = [
    'AudioFrame',
    'AudioSource',
    'CaptureSession',
    'FakeSource',
    'MicSource',
    'SourceEvent',
]
