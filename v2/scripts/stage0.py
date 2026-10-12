"""Minimal, consent-only Feishu OAuth and meeting-event probe. No environment secrets.

Run from anywhere: python3 v2/scripts/stage0.py auth-url|exchange|refresh|probe
All credentials and callback values live in v2/config.yaml (gitignored).
"""

import argparse
import json
import os
from pathlib import Path
import secrets
import stat
import sys
import tempfile
from urllib import error, parse, request

import yaml

CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"
API = "https://open.feishu.cn"
APP_TOKEN = "/open-apis/auth/v3/app_access_token/internal"
USER_TOKEN = "/open-apis/authen/v1/access_token"
REFRESH_TOKEN = "/open-apis/authen/v1/refresh_access_token"
ACTIVE = "/open-apis/vc/v1/bots/user_active_meeting"
EVENTS = "/open-apis/vc/v1/bots/events"
AUTH_URL = "https://open.feishu.cn/open-apis/authen/v1/index"


class ProbeError(Exception):
    pass


def load_config(path=CONFIG):
    try:
        if path.is_symlink():
            raise ProbeError("config.yaml must not be a symlink")
        if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise ProbeError("config.yaml permissions too broad; run chmod 600 v2/config.yaml")
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProbeError("copy v2/config.example.yaml to v2/config.yaml and chmod 600") from exc
    except yaml.YAMLError as exc:
        raise ProbeError("invalid config.yaml") from exc
    if not isinstance(data, dict) or not all(isinstance(data.get(k), dict) for k in ("app", "oauth", "probe")):
        raise ProbeError("config.yaml needs app, oauth and probe mappings")
    return data


def required(section, key):
    value = section.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ProbeError(f"missing config.yaml value: {key}")
    return value.strip()


def save_config(data, path=CONFIG):
    # No secrets are ever written anywhere except config.yaml. Atomic replace,
    # never emit config contents to stdout/stderr or a temporary file outside v2.
    fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def call(path, *, token=None, payload=None, query=None, timeout=10, opener=None):
    if query:
        path += "?" + parse.urlencode(query)
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json; charset=utf-8"
        body = json.dumps(payload).encode("utf-8")
    req = request.Request(API + path, data=body, headers=headers, method="POST" if body is not None else "GET")
    try:
        response = (opener or request.urlopen)(req, timeout=timeout)
        with response:
            data = json.load(response)
    except error.HTTPError as exc:
        raise ProbeError(f"HTTP {exc.code} ({path.split('?')[0]}); response hidden") from None
    except (error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise ProbeError(f"network or invalid JSON response ({path.split('?')[0]}); details hidden") from None
    if not isinstance(data, dict):
        raise ProbeError("unexpected API response type")
    if data.get("code") != 0:
        # Never print server msg: it can contain request parameters / user data.
        raise ProbeError(f"Feishu API error code={data.get('code')!r} ({path.split('?')[0]}); msg hidden")
    return data


def timeout(config):
    value = config["probe"].get("timeout_seconds", 10)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 1 <= value <= 60:
        raise ProbeError("probe.timeout_seconds must be 1..60")
    return value


def auth_url(config, *, save=save_config):
    app_id = required(config["app"], "id")
    redirect = required(config["oauth"], "redirect_uri")
    if not redirect.startswith("https://"):
        raise ProbeError("oauth.redirect_uri must be HTTPS and match the app console")
    config["oauth"]["state"] = secrets.token_urlsafe(32)
    config["oauth"]["code"] = ""
    config["oauth"]["returned_state"] = ""
    save(config)
    return AUTH_URL + "?" + parse.urlencode({"app_id": app_id, "redirect_uri": redirect, "state": config["oauth"]["state"]})


def app_token(config, *, api=call):
    data = api(APP_TOKEN, payload={"app_id": required(config["app"], "id"),
                                   "app_secret": required(config["app"], "secret")}, timeout=timeout(config))
    return required(data, "app_access_token")


def update_user_tokens(config, data, *, save=save_config):
    config["oauth"]["user_access_token"] = required(data, "access_token")
    if data.get("refresh_token"):
        config["oauth"]["refresh_token"] = required(data, "refresh_token")
    config["oauth"]["code"] = ""
    config["oauth"]["returned_state"] = ""
    save(config)


def exchange(config, *, api=call, save=save_config):
    oauth = config["oauth"]
    code = required(oauth, "code")
    if not secrets.compare_digest(required(oauth, "state"), required(oauth, "returned_state")):
        raise ProbeError("OAuth state mismatch; do not exchange code")
    token = app_token(config, api=api)
    data = api(USER_TOKEN, token=token, payload={"grant_type": "authorization_code", "code": code}, timeout=timeout(config))
    # v1 authen token API returns token fields inside `data`.
    update_user_tokens(config, data.get("data") if isinstance(data.get("data"), dict) else {}, save=save)


def refresh(config, *, api=call, save=save_config):
    old = required(config["oauth"], "refresh_token")
    token = app_token(config, api=api)
    data = api(REFRESH_TOKEN, token=token, payload={"grant_type": "refresh_token", "refresh_token": old}, timeout=timeout(config))
    update_user_tokens(config, data.get("data") if isinstance(data.get("data"), dict) else {}, save=save)


def event_summary(events):
    """Only schema metadata; never return text, IDs, timestamps or speaker identity."""
    if not isinstance(events, list):
        raise ProbeError("events field is not a list; inspect official contract privately")
    types = {}
    transcript_schema = {}
    for event in events:
        if not isinstance(event, dict):
            raise ProbeError("event is not an object")
        kind = event.get("event_type", event.get("type"))
        label = kind if kind in ("transcript_received", "chat_received") else "other_or_missing"
        types[label] = types.get(label, 0) + 1
        if kind == "transcript_received":
            # Fixed allowlist: unexpected keys could themselves contain private content.
            keys = ("event_type", "type", "sentence_id", "speaker", "text", "language",
                    "start_time", "end_time", "data", "payload", "content")
            transcript_schema = {k: type(event[k]).__name__ for k in keys if k in event}
    return {"counts": types, "transcript_top_level_field_types": transcript_schema}


def probe(config, *, api=call):
    token = required(config["oauth"], "user_access_token")
    limit = config["probe"].get("max_pages", 3)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
        raise ProbeError("probe.max_pages must be 1..10")
    t = timeout(config)
    active = api(ACTIVE, token=token, timeout=t)
    data = active.get("data")
    if not isinstance(data, dict):
        raise ProbeError("active meeting response lacks data object; inspect contract privately")
    meeting_id = data.get("meeting_id")
    if not isinstance(meeting_id, (str, int)) or isinstance(meeting_id, bool) or not str(meeting_id):
        return {"active_meeting_found": False, "active_data_field_types": {
            k: type(data[k]).__name__ for k in ("meeting_id", "meeting", "meeting_list") if k in data}}
    summary = {"active_meeting_found": True, "meeting_id_digits": len(str(meeting_id)), "pages": 0,
               "event_counts": {}, "transcript_top_level_field_types": {}, "more_pages": False}
    page_token = None
    seen = set()
    for _ in range(limit):
        params = {"meeting_id": str(meeting_id)}
        if page_token:
            params["page_token"] = page_token
        response = api(EVENTS, token=token, query=params, timeout=t)
        result = response.get("data")
        if not isinstance(result, dict):
            raise ProbeError("events response lacks data object; inspect contract privately")
        events = result.get("events")
        stats = event_summary(events)
        summary["pages"] += 1
        for key, count in stats["counts"].items():
            summary["event_counts"][key] = summary["event_counts"].get(key, 0) + count
        if stats["transcript_top_level_field_types"]:
            summary["transcript_top_level_field_types"] = stats["transcript_top_level_field_types"]
        nxt = result.get("page_token")
        summary["more_pages"] = bool(nxt)
        # Single bounded scan, NOT a polling cursor algorithm. Do not retry same cursor.
        if not isinstance(nxt, str) or not nxt or nxt == page_token or nxt in seen:
            break
        seen.add(nxt)
        page_token = nxt
    return summary


def main():
    parser = argparse.ArgumentParser(description="Feishu phase-0 OAuth and bounded meeting probe; secrets only in v2/config.yaml")
    parser.add_argument("command", choices=("auth-url", "exchange", "refresh", "probe"))
    args = parser.parse_args()
    try:
        config = load_config()
        if args.command == "auth-url":
            print(auth_url(config))  # URL includes public app_id and CSRF state, not a secret.
        elif args.command == "exchange":
            exchange(config)
            print("User token saved to config.yaml (token not printed).")
        elif args.command == "refresh":
            refresh(config)
            print("User token refreshed in config.yaml (token not printed).")
        else:
            print(json.dumps(probe(config), ensure_ascii=False, indent=2))
    except ProbeError as exc:
        print(f"Stage 0 failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
