"""Pending OAuth state table: state -> (open_id, intent, verifier), one-shot.

Persisted atomically so an auth restart does not invalidate links already
sent to users. Entries carry a TTL and are pruned on access.
"""

import secrets
import threading
from pathlib import Path

from .errors import AuthError
from .storage import ensure_private_directory, read_private_json, write_private_json


class StateTable:
    def __init__(self, path, clock, *, ttl_s=600):
        self.path = Path(path)
        self.clock = clock
        self.ttl_s = ttl_s
        self._lock = threading.Lock()

    def _load(self):
        value = read_private_json(self.path) if self.path.exists() else None
        if value is None:
            return {'version': 1, 'states': {}}
        if (not isinstance(value, dict) or value.get('version') != 1
                or not isinstance(value.get('states'), dict)):
            raise AuthError('oauth_invalid_local_file')
        return value

    def _save(self, value):
        write_private_json(self.path, value)

    def _prune(self, states, now):
        for state in [key for key, entry in states.items()
                      if not isinstance(entry, dict) or now >= entry.get('expires_at', 0)]:
            del states[state]

    def issue(self, open_id, intent, redirect_uri, verifier):
        state = secrets.token_urlsafe(32)
        now = self.clock()
        with self._lock:
            value = self._load()
            self._prune(value['states'], now)
            entry = {'open_id': open_id, 'intent': intent,
                     'redirect_uri': redirect_uri, 'verifier': verifier,
                     'created_at': now, 'expires_at': now + self.ttl_s,
                     'used': False}
            value['states'][state] = entry
            self._save(value)
        return {'state': state, **entry}

    def take(self, state):
        """Atomically consume a state entry (one-shot). Raises when invalid."""
        if not isinstance(state, str) or not state:
            raise AuthError('oauth_invalid_callback')
        now = self.clock()
        with self._lock:
            value = self._load()
            self._prune(value['states'], now)
            entry = value['states'].get(state)
            if entry is None:
                raise AuthError('oauth_unknown_state')
            if entry.get('used'):
                del value['states'][state]
                self._save(value)
                raise AuthError('oauth_state_used')
            entry['used'] = True
            self._save(value)
            return dict(entry)

    def release(self, state):
        """Drop a state entry (user declined)."""
        with self._lock:
            value = self._load()
            if state in value['states']:
                del value['states'][state]
                self._save(value)

    def ensure_directory(self):
        ensure_private_directory(self.path.parent)
