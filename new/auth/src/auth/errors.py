"""Auth error type; reasons mirror the legacy oauth.py set for easy diffing."""

REASONS = frozenset({
    'oauth_not_authorized', 'oauth_reauthorization_required', 'oauth_busy',
    'oauth_invalid_response', 'oauth_request_failed', 'oauth_token_rejected',
    'oauth_missing_scopes', 'oauth_excess_scopes', 'oauth_denied',
    'oauth_invalid_callback', 'oauth_invalid_local_file', 'oauth_local_write_failed',
    'oauth_invalid_open_id', 'oauth_unknown_state', 'oauth_state_expired',
    'oauth_state_used', 'oauth_subject_mismatch',
})


class AuthError(Exception):
    def __init__(self, reason, *, http_status=None, code=None, stage=None):
        self.reason = reason if reason in REASONS else 'oauth_invalid_response'
        self.http_status, self.code = http_status, code
        self.stage = stage if stage in ('code_exchange', 'refresh', 'user_identity') else None
        super().__init__(f'{self.reason} (stage={self.stage}, HTTP={http_status}, code={code}); details hidden')
