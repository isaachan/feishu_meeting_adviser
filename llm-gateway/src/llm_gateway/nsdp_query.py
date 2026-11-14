"""Read-only NSDP lookup executed outside the model process.

The caller supplies the task's trusted owner and a credential provider; the
model supplies only the release filter. Credentials never appear in arguments
or in the returned diagnostic on failure.
"""
import os
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
        if not isinstance(owner, str) or not owner:
            raise QueryError('owner_missing')
        if not isinstance(release, str) or not _RELEASE.fullmatch(release):
            raise QueryError('invalid_release')
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
                    [self.noam_bin, 'nsdp', 'releases', '--release', release],
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
        return text[:100_000]
