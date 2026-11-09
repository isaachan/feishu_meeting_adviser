"""Energy-VAD segmenter: AudioFrame stream -> bounded voice segments.

Deterministic block-based detector (no ML dependency):

- each ``frame_ms`` block is voiced when its RMS exceeds an adaptive
  threshold ``max(noise_floor * factor, min_rms)``; the noise floor is an
  EMA updated only from quiet blocks, so steady meeting noise adapts while
  speech does not inflate it;
- a segment opens after ``open_ms`` of consecutive voiced blocks and keeps
  up to ``preroll_ms`` of preceding audio;
- it closes after ``silence_ms`` of unvoiced blocks (plus ``tail_ms`` pad)
  or is force-closed at ``max_segment_ms`` (well under the 30 s GLM-ASR
  upload limit), carrying ``max_forced`` so the caller can chain prompt
  context;
- segments shorter than ``min_speech_ms`` of speech are dropped;
- ``feed_gap()`` (called on SourceEvent gap/pause) closes any open segment
  at the gap boundary — the sample timeline is never fabricated across a
  pause.
"""
from array import array
from dataclasses import dataclass
import math

from ..audio.frames import AudioFrame


@dataclass(frozen=True, slots=True)
class Segment:
    source_id: str
    index: int
    start_offset: int
    end_offset: int
    pcm: bytes  # interleaved float32 mono, [start_offset, end_offset)
    speech_ms: int
    max_forced: bool

    @property
    def duration_ms(self) -> int:
        return int((self.end_offset - self.start_offset) * 1000
                   / 16000) if self.end_offset > self.start_offset else 0


class VadSegmenter:
    """One instance per source; feed AudioFrames, collect Segments."""

    def __init__(self, source_id: str, *, rate: int = 16000,
                 frame_ms: int = 20, silence_ms: int = 650,
                 min_speech_ms: int = 250, max_segment_ms: int = 25000,
                 open_ms: int = 60, preroll_ms: int = 200, tail_ms: int = 200,
                 min_rms: float = 0.004, noise_factor: float = 1.8):
        if rate != 16000:
            raise ValueError('segmenter is fixed to 16 kHz mono input')
        self.source_id = source_id
        self.rate = rate
        self.block = rate * frame_ms // 1000
        self.silence_blocks = max(1, silence_ms // frame_ms)
        self.min_speech_blocks = max(1, min_speech_ms // frame_ms)
        self.max_blocks = max(1, max_segment_ms // frame_ms)
        self.open_blocks = max(1, open_ms // frame_ms)
        self.preroll_blocks = max(0, preroll_ms // frame_ms)
        self.tail_blocks = max(0, tail_ms // frame_ms)
        self.min_rms = min_rms
        self.noise_factor = noise_factor
        self._noise_floor = min_rms
        self._buffer = array('f')
        self._buffer_start = 0  # sample offset of buffer[0]
        self._voiced_flags: list[bool] = []
        self._open = False
        self._open_pos = 0  # buffer index where the open segment started
        self._speech_blocks = 0
        self._voiced_run = 0
        self._unvoiced_run = 0
        self._index = 0
        self._forced = False

    # -- public API ---------------------------------------------------------

    def feed(self, frame: AudioFrame) -> list[Segment]:
        if frame.channels != 1:
            raise ValueError('segmenter expects mono frames')
        samples = array('f')
        samples.frombytes(frame.data)
        if frame.sample_offset != self._buffer_start + len(self._buffer):
            # discontinuity without a reported gap: drop buffered head
            self._buffer = array('f')
            self._voiced_flags = []
            self._open = False
            self._buffer_start = frame.sample_offset
        self._buffer.extend(samples)
        return self._drain(final=False)

    def feed_gap(self) -> list[Segment]:
        """A pause/discontinuity: close any open segment at the boundary."""
        segments = self._close_open(force_index=True) if self._open else []
        self._buffer = array('f')
        self._voiced_flags = []
        self._speech_blocks = 0
        self._voiced_run = 0
        self._unvoiced_run = 0
        return segments

    def flush(self) -> list[Segment]:
        """End of stream: emit any open segment."""
        segments = self._close_open(force_index=False) if self._open else []
        self._trim()
        return segments

    # -- internals ----------------------------------------------------------

    def _block_rms(self, start: int) -> float:
        chunk = self._buffer[start:start + self.block]
        if not chunk:
            return 0.0
        total = 0.0
        for v in chunk:
            total += v * v
        return math.sqrt(total / len(chunk))

    def _threshold(self) -> float:
        return max(self.min_rms, self._noise_floor * self.noise_factor)

    def _drain(self, final: bool) -> list[Segment]:
        segments: list[Segment] = []
        while len(self._voiced_flags) * self.block + self.block <= len(self._buffer):
            idx = len(self._voiced_flags)
            pos = idx * self.block
            if pos + self.block > len(self._buffer):
                break
            rms = self._block_rms(pos)
            voiced = rms > self._threshold()
            self._voiced_flags.append(voiced)
            if voiced:
                self._voiced_run += 1
                self._unvoiced_run = 0
                if not self._open:
                    # quiet-side adaptation only; speech never inflates the floor
                    self._noise_floor = (self._noise_floor * 0.98
                                         + min(rms, self._threshold()) * 0.02)
            else:
                self._unvoiced_run += 1
                self._voiced_run = 0
                # adapt noise floor from quiet blocks only
                self._noise_floor = self._noise_floor * 0.9 + rms * 0.1

            if not self._open:
                if self._voiced_run >= self.open_blocks:
                    self._open = True
                    self._forced = False
                    self._speech_blocks = self._voiced_run
                    self._open_pos = max(0, pos - (self._voiced_run - 1) * self.block
                                         - self.preroll_blocks * self.block)
            else:
                if voiced:
                    self._speech_blocks += 1
                open_blocks = (pos + self.block - self._open_pos) // self.block
                if open_blocks >= self.max_blocks:
                    self._forced = True
                    segments += self._close_open(force_index=True)
                    self._open = True  # reopen immediately at cut point
                    self._forced = False
                    self._open_pos = pos + self.block
                    self._speech_blocks = 1
                    self._unvoiced_run = 0
                elif self._unvoiced_run >= self.silence_blocks:
                    segments += self._close_open(force_index=False)
                    self._open = False
        self._trim()
        return segments

    def _close_open(self, force_index: bool) -> list[Segment]:
        idx = len(self._voiced_flags)
        if force_index:
            end_pos = idx * self.block
        else:
            # segment ends where the trailing silence began, plus tail pad
            end_pos = max(self._open_pos + self.min_speech_blocks * self.block,
                          (idx - self.silence_blocks) * self.block
                          + self.tail_blocks * self.block)
        end_pos = max(0, min(end_pos, len(self._buffer)))
        if self._speech_blocks >= self.min_speech_blocks and end_pos > self._open_pos:
            self._index += 1
            pcm = self._buffer[self._open_pos:end_pos].tobytes()
            start_offset = self._buffer_start + self._open_pos
            end_offset = self._buffer_start + end_pos
            segment = Segment(self.source_id, self._index, start_offset,
                              end_offset, pcm,
                              self._speech_blocks * (self.block * 1000 // self.rate),
                              self._forced)
            self._open = False
            self._speech_blocks = 0
            return [segment]
        self._open = False
        self._speech_blocks = 0
        return []

    def _trim(self) -> None:
        keep_from = self._open_pos if self._open else len(self._buffer)
        if not self._open:
            self._buffer_start += len(self._buffer)
            self._buffer = array('f')
            self._voiced_flags = []
            return
        if keep_from > 0:
            self._buffer = self._buffer[keep_from:]
            self._buffer_start += keep_from
            self._voiced_flags = self._voiced_flags[keep_from // self.block:]
            self._open_pos = 0
