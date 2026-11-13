"""Adviser runtime settings (single-user defaults; config file in stage 3)."""
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AdviserSettings:
    role_file: Path | None = None
    remote_url: str = 'http://127.0.0.1:8377'
    remote_backend: str = 'opencode'
    model_timeout_seconds: float = 90
    batch_seconds: float = 2
    context_characters: int = 30000
    freshness_seconds: int = 120
    reply_evidence_seconds: int = 600
    reply_interval_seconds: float = 10
    idle_timeout_s: int = 900
    poll_interval: float = 0.5
    nsdp_refresh_seconds: float = 900
    nsdp_retry_seconds: float = 60
    nsdp_nag_seconds: float = 300
