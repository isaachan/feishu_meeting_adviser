"""Shared Feishu HTTP and local configuration helpers. Credentials never read from env."""

import json
import re
from pathlib import Path
import stat
from urllib import error, parse, request

import yaml

CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"
API = "https://open.feishu.cn"
TENANT_TOKEN = "/open-apis/auth/v3/tenant_access_token/internal"


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
    if not isinstance(data, dict) or not all(isinstance(data.get(k), dict) for k in ("app", "probe", "lookup")):
        raise ProbeError("config.yaml needs app, probe and lookup mappings")
    return data


def required(section, key):
    value = section.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ProbeError(f"missing config.yaml value: {key}")
    return value.strip()


_SAFE_ID = re.compile(r"[A-Za-z0-9_.:-]{1,128}\Z")


def diagnostic_details(data, headers, *, secrets=()):
    """Explicit local-only error hint; never include raw token, cursor or meeting ID."""
    msg = data.get("msg")
    if not isinstance(msg, str):
        msg = "<not provided>"
    for value in secrets:
        if isinstance(value, str) and value:
            msg = msg.replace(value, "[REDACTED]")
    msg = msg.replace("\r", " ").replace("\n", " ")[:500]
    ids = {}
    for key in ("request_id", "log_id"):
        value = data.get(key)
        if isinstance(value, str) and _SAFE_ID.fullmatch(value):
            ids[key] = value
    for key in ("X-Request-Id", "X-Tt-Logid"):
        value = headers.get(key) if headers is not None else None
        if isinstance(value, str) and _SAFE_ID.fullmatch(value):
            ids[key.lower()] = value
    return {"msg": msg, "ids": ids}


def call(path, *, token=None, payload=None, query=None, timeout=10, opener=None,
         diagnose=False, redact_values=()):
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
    secrets = (token, (payload or {}).get("app_secret") if isinstance(payload, dict) else None,
               *(str(v) for v in (query or {}).values()), *redact_values)
    try:
        response = (opener or request.urlopen)(req, timeout=timeout)
        with response:
            data = json.load(response)
            response_headers = response.headers
            status = getattr(response, "status", None)
    except error.HTTPError as exc:
        detail = ""
        if diagnose:
            try:
                err_data = json.load(exc)
                if isinstance(err_data, dict):
                    detail = "; diagnostic=" + json.dumps(diagnostic_details(err_data, exc.headers, secrets=secrets), ensure_ascii=False)
            except (ValueError, OSError):
                pass
        raise ProbeError(f"HTTP {exc.code} ({path.split('?')[0]}){detail}; response body hidden") from None
    except (error.URLError, TimeoutError, OSError, ValueError):
        raise ProbeError(f"network or invalid JSON response ({path.split('?')[0]}); details hidden") from None
    if not isinstance(data, dict):
        raise ProbeError("unexpected API response type")
    if data.get("code") != 0:
        # Never print server msg: it can contain request parameters / user data.
        code = data.get("code")
        safe_code = code if type(code) is int else "unknown"
        if diagnose:
            details = diagnostic_details(data, response_headers, secrets=secrets)
            raise ProbeError(f"Feishu API error code={safe_code}, HTTP {status} ({path.split('?')[0]}); diagnostic="
                             + json.dumps(details, ensure_ascii=False))
        raise ProbeError(f"Feishu API error code={safe_code} ({path.split('?')[0]}); msg hidden")
    return data


def timeout(config):
    value = config["probe"].get("timeout_seconds", 10)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 1 <= value <= 60:
        raise ProbeError("probe.timeout_seconds must be 1..60")
    return value


def tenant_token(config, *, api=call):
    data = api(TENANT_TOKEN, payload={"app_id": required(config["app"], "id"),
                                      "app_secret": required(config["app"], "secret")}, timeout=timeout(config))
    return required(data, "tenant_access_token")
