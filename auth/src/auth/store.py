"""Per-open_id credential store: marker-first refresh state, atomic writes.

Ported from legacy oauth.py OAuthManager storage/token-record logic, changed to
one record file per open_id under ``<data>/credentials/`` (multi-user cloud).
"""

import math
import re
from contextlib import contextmanager
from pathlib import Path

from .constants import MAX_FILE_BYTES, SCOPES
from .errors import AuthError
from .storage import FileLock, ensure_private_directory, read_private_json, write_private_json

RECORD_STATES = ('ready', 'refreshing', 'reauthorization_required')
OPEN_ID_TEXT = re.compile(r'ou_[A-Za-z0-9_-]{1,61}\Z')
_SECRET_TEXT = re.compile(r'[^\x00-\x20\x7f]{1,16000}\Z')


def valid_open_id(value) -> bool:
    return isinstance(value, str) and bool(OPEN_ID_TEXT.fullmatch(value))


class CredentialStore:
    def __init__(self, directory):
        self.directory = Path(directory)

    def _paths(self, open_id):
        if not valid_open_id(open_id):
            raise AuthError('oauth_invalid_open_id')
        base = self.directory / open_id
        return base.with_suffix('.json'), base.with_suffix('.lock')

    @contextmanager
    def locked(self, open_id):
        _, lock_path = self._paths(open_id)
        ensure_private_directory(self.directory)
        with FileLock(lock_path):
            yield

    def read(self, open_id):
        path, _ = self._paths(open_id)
        value = read_private_json(path)
        if value is None:
            raise AuthError('oauth_not_authorized')
        if (not isinstance(value, dict) or value.get('version') != 1 or value.get('open_id') != open_id
                or value.get('state') not in RECORD_STATES):
            raise AuthError('oauth_invalid_local_file')
        return value

    def write(self, open_id, value):
        path, _ = self._paths(open_id)
        write_private_json(path, value)

    def exists(self, open_id) -> bool:
        path, _ = self._paths(open_id)
        return path.exists()

    def token_record(self, result, *, started, open_id, subject_hash):
        """Validate a provider token response and build the stored record."""
        for key in ('access_token', 'refresh_token'):
            if not isinstance(result.get(key), str) or not _SECRET_TEXT.fullmatch(result[key]):
                raise AuthError('oauth_invalid_response')
        for key in ('expires_in', 'refresh_token_expires_in'):
            if type(result.get(key)) is not int or not 0 < result[key] <= 366 * 86400:
                raise AuthError('oauth_invalid_response')
        if result.get('token_type') != 'Bearer' or not isinstance(result.get('scope'), str):
            raise AuthError('oauth_invalid_response')
        scopes = set(result['scope'].split())
        if not set(SCOPES) <= scopes:
            raise AuthError('oauth_missing_scopes')
        if scopes - set(SCOPES) - {'user_profile'}:
            raise AuthError('oauth_excess_scopes')
        return {'version': 1, 'open_id': open_id,
                'state': 'ready', 'subject_hash': subject_hash,
                'access_token': result['access_token'], 'refresh_token': result['refresh_token'],
                'expires_at': started + result['expires_in'],
                'refresh_expires_at': started + result['refresh_token_expires_in'],
                'scopes': sorted(scopes), 'refresh_margin': min(300, result['expires_in'] / 2)}

    def marker(self, open_id, state, subject_hash):
        return {'version': 1, 'open_id': open_id,
                'state': state, 'subject_hash': subject_hash}

    def validated_ready(self, value):
        """Validate the ready-record fields used by the refresh decision."""
        try:
            valid_time = (type(value['expires_at']) in (int, float) and math.isfinite(value['expires_at'])
                and type(value['refresh_margin']) in (int, float) and math.isfinite(value['refresh_margin']))
            if not valid_time or not set(SCOPES) <= set(value['scopes']) or not _SECRET_TEXT.fullmatch(value['access_token']):
                raise ValueError()
            if (not _SECRET_TEXT.fullmatch(value['refresh_token'])
                    or type(value['refresh_expires_at']) not in (int, float)
                    or not math.isfinite(value['refresh_expires_at'])):
                raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise AuthError('oauth_invalid_local_file') from None


_NSDP_TOKEN = re.compile(r'[!-~]{8,512}\Z')


class ProfileStore:
    """Per-open_id identity profiles under ``<data>/profiles/`` (v1.3).

    Persisted from the OAuth callback's user_info summary; read via the
    internal API by the advisor identity directory. Never holds tokens.
    """

    def __init__(self, directory):
        self.directory = Path(directory)

    def _paths(self, open_id):
        if not valid_open_id(open_id):
            raise AuthError('oauth_invalid_open_id')
        base = self.directory / open_id
        return base.with_suffix('.json'), base.with_suffix('.lock')

    @contextmanager
    def locked(self, open_id):
        _, lock_path = self._paths(open_id)
        ensure_private_directory(self.directory)
        with FileLock(lock_path):
            yield

    def read(self, open_id):
        path, _ = self._paths(open_id)
        value = read_private_json(path)
        if value is None:
            return None
        if (not isinstance(value, dict) or value.get('version') != 1
                or value.get('open_id') != open_id):
            raise AuthError('oauth_invalid_local_file')
        return {key: value.get(key)
                for key in ('open_id', 'name', 'en_name', 'email', 'updated_at')}

    def write(self, open_id, info, *, now):
        path, _ = self._paths(open_id)
        record = {'version': 1, 'open_id': open_id,
                  'name': info.get('name'), 'en_name': info.get('en_name'),
                  'email': info.get('email'), 'updated_at': now}
        write_private_json(path, record)


class NsdpCredentialStore:
    """Per-open_id NSDP tokens under ``credentials/nsdp/`` (CONTRACT_AUTH §8).

    Long-lived personal tokens submitted via the HTTPS form: no refresh
    semantics, no state machine. Invalidated by deletion (next read is
    ``not_authorized`` so the organizer gets a fresh submit link).
    """

    def __init__(self, directory):
        self.directory = Path(directory)

    def _paths(self, open_id):
        if not valid_open_id(open_id):
            raise AuthError('oauth_invalid_open_id')
        base = self.directory / open_id
        return base.with_suffix('.json'), base.with_suffix('.lock')

    @contextmanager
    def locked(self, open_id):
        _, lock_path = self._paths(open_id)
        ensure_private_directory(self.directory)
        with FileLock(lock_path):
            yield

    def read(self, open_id):
        path, _ = self._paths(open_id)
        value = read_private_json(path)
        if value is None:
            return None
        if (not isinstance(value, dict) or value.get('version') != 1
                or value.get('provider') != 'nsdp' or value.get('open_id') != open_id
                or not isinstance(value.get('token'), str)
                or not _NSDP_TOKEN.fullmatch(value['token'])
                or type(value.get('updated_at')) not in (int, float)):
            raise AuthError('oauth_invalid_local_file')
        return value

    def write(self, open_id, token, *, now):
        path, _ = self._paths(open_id)
        if not isinstance(token, str) or not _NSDP_TOKEN.fullmatch(token):
            raise AuthError('nsdp_invalid_token')
        write_private_json(path, {'version': 1, 'provider': 'nsdp', 'open_id': open_id,
                                  'token': token, 'updated_at': now})

    def delete(self, open_id):
        path, _ = self._paths(open_id)
        try:
            path.unlink(missing_ok=True)
        except OSError:
            raise AuthError('oauth_local_write_failed') from None
