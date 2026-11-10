"""Adviser domain errors."""

MODEL_ERRORS = frozenset({
    'model_authentication_required', 'model_context_budget', 'model_invalid_decision',
    'model_invalid_evidence', 'model_role_unavailable', 'model_unavailable',
    'model_disconnected', 'model_protocol', 'model_tool_request', 'model_stopped',
    'model_timeout', 'model_failed', 'model_subscription_required',
    'model_tools_unavailable', 'unknown_model_error',
})


class StorageError(Exception):
    """Safe local error, never includes database contents or a sensitive path."""


class CapacityError(StorageError):
    pass


class StateError(StorageError):
    """Illegal state machine transition (STATE_MACHINES.md)."""


class ModelError(Exception):
    def __init__(self, reason):
        self.reason = reason if isinstance(reason, str) and reason in MODEL_ERRORS else 'unknown_model_error'
        super().__init__(f'{self.reason}; model/server details hidden')
