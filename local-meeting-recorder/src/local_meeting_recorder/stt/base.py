"""Transcriber interface shared by remote/local implementations."""
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    text: str
    request_id: str
    latency_ms: int
    model: str


class TranscriberError(RuntimeError):
    """STT failure with a coarse machine-readable reason."""

    def __init__(self, reason: str, message: str = ''):
        super().__init__(message or reason)
        self.reason = reason


class Transcriber(Protocol):
    """Final-text-only transcription of one mono float32 PCM segment."""

    def transcribe(self, pcm: bytes, *, rate: int = 16000, prompt: str | None = None,
                   request_id: str | None = None) -> TranscriptionResult: ...
