"""Join the app bot to a meeting and persist ONLY its verified join response locally."""

import os
from pathlib import Path
import stat
import tempfile
import time

import yaml

import feishu_client as client
import lookup_meeting_id

JOIN = "/open-apis/vc/v1/bots/join"
SESSION = Path(__file__).resolve().parents[1] / "session.yaml"  # generated, gitignored


def meeting_id(value):
    if not isinstance(value, str) or not value.isascii() or not value.isdecimal() or len(value) <= 9:
        raise client.ProbeError("join response missing long numeric data.meeting.id")
    return value


def join(config, *, api=client.call, token=None):
    number = lookup_meeting_id.meeting_number(config)
    settings = config.get("join", {})
    if not isinstance(settings, dict):
        raise client.ProbeError("join must be a mapping")
    password = settings.get("password", "")
    if not isinstance(password, str):
        raise client.ProbeError("join.password must be a string")
    payload = {"join_type": 1, "join_identify": {"meeting_no": number}}
    if password:
        payload["password"] = password
    if token is None:
        token = client.tenant_token(config, api=api)
    result = api(JOIN, token=token, payload=payload, timeout=client.timeout(config))
    data = result.get("data")
    meeting = data.get("meeting") if isinstance(data, dict) else None
    if not isinstance(meeting, dict) or meeting.get("meeting_no") != number:
        raise client.ProbeError("join response meeting number missing/mismatch; no session saved")
    return {"meeting_no": number, "meeting_id": meeting_id(meeting.get("id"))}


def save_session(session, path=SESSION):
    # Atomic mode-600 write into v2 only. Never modify config.yaml or log secrets.
    fd, temp = tempfile.mkstemp(prefix=".session-", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            yaml.safe_dump(session, stream, sort_keys=False)
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def load_session(config, path=SESSION):
    if path.is_symlink():
        raise client.ProbeError("session.yaml must not be a symlink")
    try:
        if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise client.ProbeError("session.yaml permissions too broad")
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, yaml.YAMLError) as exc:
        raise client.ProbeError("no valid bot session; run stage0.py join first") from exc
    if not isinstance(data, dict) or data.get("meeting_no") != lookup_meeting_id.meeting_number(config):
        raise client.ProbeError("session meeting number mismatch; run stage0.py join for this meeting")
    meeting_id(data.get("meeting_id"))
    return data["meeting_id"]


def join_and_save(config, *, api=client.call, path=SESSION, now_fn=time.time):
    # A failed attempt must not leave behind a previous meeting's usable ID.
    lookup_meeting_id.meeting_number(config)
    if path.is_symlink():
        raise client.ProbeError("session.yaml must not be a symlink")
    path.unlink(missing_ok=True)
    session = join(config, api=api)
    session["joined_at"] = int(now_fn())
    save_session(session, path)
    return session["meeting_id"]
