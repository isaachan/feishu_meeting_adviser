"""Test doubles shared across advisor unit tests."""


class MockNsdp:
    """No-op NSDP service for tests that do not exercise NSDP behaviour.

    Production always wires a real ``NsdpService``; tests pass this double so
    product code never needs ``nsdp=None`` guards. Methods record calls so a
    test can assert the NSDP path stayed dormant.
    """

    def __init__(self):
        self.owner_notices = []
        self.jira_notices = []

    def submit_url(self, open_id):
        return f'https://auth.internal/nsdp/submit?state={open_id}'

    def notify_owner(self, session_id, owner_open_id, *, dedupe_s=300.0):
        self.owner_notices.append((session_id, owner_open_id, dedupe_s))

    def notify_jira_owner(self, session_id, owner_open_id, *, dedupe_s=300.0):
        self.jira_notices.append((session_id, owner_open_id, dedupe_s))
