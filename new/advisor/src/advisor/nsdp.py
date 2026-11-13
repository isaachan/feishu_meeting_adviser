"""Organizer NSDP credential gate and advisor-side data fetch.

The organizer's NSDP token only ever lives here: advisor fetches
version-plan briefs via the noam CLI (token passed through the subprocess
environment only) and stores them as session material. Tokens never enter
IM text, the recorder, or the model sandbox (CONTRACT_AUTH §8).
"""
import hashlib
import json
import logging
import os
import re
import subprocess
import threading
import time

from .auth_client import AuthorizationRequired, AuthUnavailable

LOG = logging.getLogger(__name__)

_VERSION_CODE = re.compile(r'\bBL\d+(?:\.\d+)+\b')
_PLATFORM_CODE = re.compile(r'\bNT[0-9]\b')
_MAX_CLUES = 3


class NsdpUnauthorized(Exception):
    """NSDP rejected the token (HTTP 401): reauthorization required."""


class NsdpUnavailable(Exception):
    """NSDP/noam unreachable or returned an error unrelated to authorization."""


def extract_clues(text) -> list[str]:
    """Targeted query clues from material text: version codes, then platform codes."""
    clues: list[str] = []
    for pattern in (_VERSION_CODE, _PLATFORM_CODE):
        for match in pattern.findall(text or ''):
            if match not in clues:
                clues.append(match)
    return clues[:_MAX_CLUES]


def _looks_unauthorized(stdout: str, stderr: str) -> bool:
    blob = (stdout or '') + (stderr or '')
    if not blob:
        return False
    stripped = blob.strip()
    try:
        value = json.loads(stripped)
        if isinstance(value, dict) and value.get('status_code') == 401:
            return True
    except ValueError:
        pass
    return '401' in blob


class NsdpClient:
    """Runs ``noam nsdp …`` with the user token in the environment only."""

    def __init__(self, *, noam_bin='noam', timeout_s=60.0, per_call_chars=4000,
                 cache_ttl_s=600.0, clock=time.monotonic, run=None):
        self.noam_bin = noam_bin
        self.timeout_s = timeout_s
        self.per_call_chars = per_call_chars
        self.cache_ttl_s = cache_ttl_s
        self._clock = clock
        self._cache: dict = {}
        self._cache_lock = threading.Lock()
        self._run = run or self._run_subprocess

    def _run_subprocess(self, argv, env, timeout=None):
        return subprocess.run(argv, env=env, capture_output=True, text=True,
                              encoding='utf-8', errors='replace',
                              timeout=timeout if timeout is not None else self.timeout_s,
                              stdin=subprocess.DEVNULL)

    def brief(self, token, clues_text='', *, timeout_s=None) -> str:
        """Returns a bounded text digest of the version plan for this token.

        With clues: targeted ``releases --release <clue>`` calls only — the
        whole-catalog ``nsdp info`` measured 30s+ against the real NSDP
        (2026-11-12), which used to blow the join-time budget; targeted calls
        run in ~1.5s. ``info`` remains the no-clue fallback. ``timeout_s``
        overrides the per-call budget (join path passes a short budget so the
        T7 degrade kicks in fast; background refresh uses the full default).

        Successful briefs are cached per (token, clues) for ``cache_ttl_s``
        (join + supervisor refresh + re-joins share it); failures are never
        cached.
        """
        cache_key = (hashlib.sha256(token.encode()).hexdigest(), clues_text)
        now = self._clock()
        with self._cache_lock:
            cached = self._cache.get(cache_key)
            if cached is not None and now < cached[0]:
                return cached[1]
        env = dict(os.environ)
        env['NSDP_TOKEN'] = token
        clues = extract_clues(clues_text)
        commands = ([('nsdp', 'releases', '--release', clue) for clue in clues]
                    or [('nsdp', 'info')])
        sections = []
        for args in commands:
            argv = [self.noam_bin, *args]
            started = time.monotonic()
            budget = timeout_s if timeout_s is not None else self.timeout_s
            try:
                completed = self._run(argv, env, timeout=budget)
            except subprocess.TimeoutExpired:
                LOG.warning('noam timeout: args=%s budget=%.0fs', ' '.join(args),
                            budget)
                raise NsdpUnavailable('timeout') from None
            except OSError:
                raise NsdpUnavailable('spawn_failed') from None
            elapsed = time.monotonic() - started
            stdout = completed.stdout or ''
            stderr = completed.stderr or ''
            if completed.returncode != 0:
                if _looks_unauthorized(stdout, stderr):
                    raise NsdpUnauthorized()
                LOG.warning('noam failed: args=%s exit=%s elapsed=%.1fs stderr=%s',
                            ' '.join(args), completed.returncode, elapsed,
                            stderr[:200])
                raise NsdpUnavailable(f'exit={completed.returncode}')
            text = stdout.strip()
            if elapsed > 5:
                LOG.info('noam slow call: args=%s elapsed=%.1fs', ' '.join(args), elapsed)
            if text:
                sections.append('$ noam ' + ' '.join(args) + '\n'
                                + text[:self.per_call_chars])
        if not sections:
            raise NsdpUnavailable('empty')
        header = '、'.join(clues) if clues else '概览'
        brief = f'NSDP 版本计划（{header}）\n\n' + '\n\n'.join(sections)
        with self._cache_lock:
            self._cache[cache_key] = (now + self.cache_ttl_s, brief)
        return brief


class StubNsdpClient:
    """Deterministic client for integration beds (token 'bad-token' simulates 401)."""

    def __init__(self, text='NSDP 版本计划（桩）\n\n$ nsdp info\n桩平台 桩版本 桩车型 桩里程碑'):
        self.text = text
        self.calls: list[tuple[str, str]] = []
        self.timeouts: list = []

    def brief(self, token, clues_text='', *, timeout_s=None):
        self.calls.append((token, clues_text))
        self.timeouts.append(timeout_s)
        if token == 'bad-token':
            raise NsdpUnauthorized()
        return self.text


class NsdpService:
    """NSDP gate + prefetch + hold maintenance, shared by api and supervisor."""

    def __init__(self, auth_client, client, store, *, clock=time.time):
        self.auth = auth_client
        self.client = client
        self.store = store
        self.clock = clock

    def require_token(self, open_id) -> str:
        """Raises AuthorizationRequired(provider='nsdp') / AuthUnavailable."""
        try:
            return self.auth.get_nsdp_token(open_id)['token']
        except KeyError:
            raise AuthUnavailable('protocol') from None

    def submit_url(self, open_id) -> str:
        return self.auth.nsdp_submit_url(open_id)

    def fetch(self, open_id, clues_text='', *, budget_s=None) -> str:
        """Prefetch brief text; a rejected token is invalidated and re-raised
        as AuthorizationRequired (fresh submit link). ``budget_s`` caps each
        noam call (join path uses a short budget, refresh uses the default)."""
        token = self.require_token(open_id)
        try:
            return self.client.brief(token, clues_text, timeout_s=budget_s)
        except NsdpUnauthorized:
            self.auth.invalidate_nsdp(open_id)
            raise AuthorizationRequired(self.submit_url(open_id),
                                        provider='nsdp') from None

    def prefetch(self, open_id, session_id, clues_text='') -> str:
        text = self.fetch(open_id, clues_text)
        self.store.update_nsdp_material(session_id, text, now=self.clock())
        return text

    def notify_owner(self, session_id, owner_open_id, *, dedupe_s=300.0):
        """Queue the re-submission DM to the organizer via the outbox."""
        try:
            url = self.submit_url(owner_open_id)
        except Exception:  # noqa: BLE001 - nag must never break the loop
            LOG.exception('nsdp submit-url failed for %s', owner_open_id)
            return
        self.store.insert_owner_notice(
            session_id, owner_open_id,
            '你的 NSDP token 缺失或已失效，会参谋已暂停本场会议的判断。'
            '请打开链接重新提交（10 分钟内有效，一次性）：\n' + url,
            now=self.clock(), dedupe_s=dedupe_s)
