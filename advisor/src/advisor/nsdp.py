"""Organizer re-authorization notices (NSDP and Jira).

Actual NSDP lookups now run in the gateway's scoped tool. Advisor keeps only
the outbox notification path: when a gateway tool reports missing/expired
credentials, the organizer is DMed a fresh one-time submit link. Tokens never
enter advisor text, the recorder, or the model sandbox (CONTRACT_AUTH §8).
"""
import logging
import time

LOG = logging.getLogger(__name__)


class NsdpService:
    """Queues owner re-authorization notices through the shared outbox."""

    def __init__(self, auth_client, store, *, clock=time.time):
        self.auth = auth_client
        self.store = store
        self.clock = clock

    def submit_url(self, open_id) -> str:
        return self.auth.nsdp_submit_url(open_id)

    def notify_owner(self, session_id, owner_open_id, *, dedupe_s=300.0):
        """Queue the NSDP re-submission DM to the organizer via the outbox."""
        try:
            url = self.submit_url(owner_open_id)
        except Exception:  # noqa: BLE001 - nag must never break the loop
            LOG.exception('nsdp submit-url failed for %s', owner_open_id)
            return
        self.store.insert_owner_notice(
            session_id, owner_open_id,
            '暂时无法查询版本计划。请打开链接更新凭据（10 分钟内有效）：\n' + url,
            now=self.clock(), dedupe_s=dedupe_s)

    def notify_jira_owner(self, session_id, owner_open_id, *, dedupe_s=300.0):
        try:
            url = self.auth.jira_submit_url(owner_open_id)
        except Exception:
            LOG.exception('jira submit-url failed for %s', owner_open_id)
            return
        self.store.insert_owner_notice(
            session_id, owner_open_id,
            '暂时无法查询 Jira。请打开链接更新凭据（10 分钟内有效）：\n' + url,
            now=self.clock(), dedupe_s=dedupe_s, provider='jira')
