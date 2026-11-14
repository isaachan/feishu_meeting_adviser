"""Read-only NSDP lookup executed outside the model process.

The caller supplies the task's trusted owner and a credential provider; the
model supplies only the release filter. Credentials never appear in arguments
or in the returned diagnostic on failure.
"""
import os
import json
import re
import subprocess
import tempfile
from pathlib import Path


_RELEASE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z')


class QueryError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class NsdpQuery:
    def __init__(self, credential_provider, *, noam_bin='noam', timeout_s=60,
                 run=subprocess.run):
        self.credential_provider = credential_provider
        self.noam_bin = noam_bin
        self.timeout_s = timeout_s
        self.run = run

    def releases(self, owner: str, release: str) -> str:
        if not isinstance(release, str) or not _RELEASE.fullmatch(release):
            raise QueryError('invalid_release')
        return self._execute(owner, ['nsdp', 'releases', '--release', release])[:100_000]

    def info(self, owner: str) -> dict:
        """Fetch the organizer's complete NSDP catalog without truncating JSON."""
        text = self._execute(owner, ['nsdp', 'info'])
        if len(text.encode('utf-8')) > 2 * 1024 * 1024:
            raise QueryError('output_too_large')
        try:
            value = json.loads(text)
        except ValueError:
            raise QueryError('output_invalid') from None
        if (not isinstance(value, dict) or not isinstance(value.get('tenants'), list)
                or not isinstance(value.get('platforms'), list)
                or any(not isinstance(item, dict) for item in value['platforms'])):
            raise QueryError('output_invalid')
        return value

    def _execute(self, owner: str, args: list[str]) -> str:
        if not isinstance(owner, str) or not owner:
            raise QueryError('owner_missing')
        token = self.credential_provider(owner)
        if not isinstance(token, str) or not token:
            raise QueryError('authorization_required')
        # noam may cache responses. Isolate both its configuration and cache
        # per invocation so a query cannot reuse another organizer's data.
        with tempfile.TemporaryDirectory(prefix='gateway-nsdp-') as directory:
            env = dict(os.environ)
            env['HOME'] = directory
            for category in ('CONFIG', 'DATA', 'CACHE', 'STATE'):
                env['XDG_' + category + '_HOME'] = str(Path(directory) / category.lower())
            env['NSDP_TOKEN'] = token
            try:
                result = self.run(
                    [self.noam_bin, *args],
                    env=env, capture_output=True, text=True, encoding='utf-8',
                    errors='replace', timeout=self.timeout_s, stdin=subprocess.DEVNULL)
            except subprocess.TimeoutExpired:
                raise QueryError('timeout') from None
            except OSError:
                raise QueryError('unavailable') from None
        stdout, stderr = result.stdout or '', result.stderr or ''
        if result.returncode:
            # Preserve the existing noam integration's 401 detection behavior;
            # caller handles invalidation and authorization notification.
            raise QueryError('authorization_required' if '401' in stdout + stderr
                             else 'unavailable')
        text = stdout.strip()
        if not text:
            raise QueryError('empty')
        if token in text:
            raise QueryError('unavailable')
        return text
