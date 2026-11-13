"""Normalized audio frames shared by capture, retention and STT layers."""
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class AudioFrame:
    """A chunk of interleaved float32 PCM from one source.

    ``sample_offset`` counts captured frames since the source started and
    pauses are NOT part of the sample timeline: gaps are reported as
    SourceEvent entries and consumers combine both to reason about time.
    """

    source_id: str
    seq: int
    sample_offset: int
    rate: int
    channels: int
    data: bytes
    captured_monotonic: float

    @property
    def frames(self) -> int:
        if self.channels <= 0:
            return 0
        return len(self.data) // 4 // self.channels

    @property
    def end_offset(self) -> int:
        return self.sample_offset + self.frames


@dataclass(frozen=True, slots=True)
class SourceEvent:
    """Lifecycle/health markers for one source, persisted to events.jsonl."""

    source_id: str
    type: str
    captured_monotonic: float
    detail: dict = field(default_factory=dict)
