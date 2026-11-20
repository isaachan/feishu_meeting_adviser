"""Adviser runtime settings (single-user defaults; config file in stage 3)."""
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AdviserSettings:
    remote_url: str = 'http://127.0.0.1:8377'
    remote_backend: str = 'opencode'
    model_timeout_seconds: float = 90
    batch_seconds: float = 2
    context_characters: int = 30000
    freshness_seconds: int = 240
    reply_evidence_seconds: int = 600
    reply_interval_seconds: float = 10
    idle_timeout_s: int = 900
    poll_interval: float = 0.5
    observe_poll_s: float = 15
    observe_timeout_s: float = 10
    max_observation_age_s: float = 60
    approval_ttl_s: float = 1800
    drain_deadline_s: float = 600
    reply_validity_s: float = 3600
    nsdp_refresh_seconds: float = 900
    nsdp_retry_seconds: float = 60
    nsdp_nag_seconds: float = 300
