"""Incrementally read meeting captions page by page; retain only normalized sentences.

Run from v2/: venv/bin/python3 scripts/stream_transcripts.py
User-provided settings and credentials are read ONLY from config.yaml. Logs and state
are local, gitignored, mode 600; meeting text is sensitive. Ctrl-C stops polling,
not the bot itself. The durable log is at-least-once; terminal output is best effort.
"""

from collections import Counter
import fcntl
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import time

import bot_join
import feishu_client as client
import stage0

BASE = Path(__file__).resolve().parents[1] / "output"


def options(config):
    values = config.get("stream", {})
    if not isinstance(values, dict):
        raise client.ProbeError("stream must be a mapping in config.yaml")

    def number(key, default, minimum, maximum):
        value = values.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not minimum <= value <= maximum:
            raise client.ProbeError(f"stream.{key} must be {minimum}..{maximum}")
        return value

    if not isinstance(values.get("capture_failed_page", False), bool):
        raise client.ProbeError("stream.capture_failed_page must be true or false")
    return {
        "capture_failed_page": values.get("capture_failed_page", False),
        "poll_seconds": number("poll_seconds", 2, 1, 30),
        "request_seconds": number("request_seconds", 1, 0.7, 30),  # 100 events requests/min limit
        "initial_lookback_seconds": int(number("initial_lookback_seconds", 600, 30, 86400)),
        "overlap_seconds": int(number("overlap_seconds", 120, 10, 3600)),
        "page_size": int(number("page_size", 100, 20, 100)),
        "max_pages_per_cycle": int(number("max_pages_per_cycle", 10, 1, 100)),
        "max_seen": int(number("max_seen", 5000, 100, 50000)),
        "max_log_bytes": int(number("max_log_bytes", 10_000_000, 10_000, 100_000_000)),
    }


def _private_dir(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.is_symlink() or path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise client.ProbeError("stream output directory must not be a symlink and must have mode 700")


def _write_state(path, state):
    fd, tmp = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(state, out, ensure_ascii=False)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _load_state(path, meeting_id, *, now, initial_lookback):
    if path.is_symlink():
        raise client.ProbeError("state file must not be a symlink")
    if path.exists():
        if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise client.ProbeError("state file permissions too broad")
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise client.ProbeError("invalid stream state; do not delete before investigating") from exc
        if (not isinstance(state, dict) or state.get("meeting_id") != meeting_id
                or not isinstance(state.get("seen"), dict)
                or not isinstance(state.get("query_start"), int)
                or state.get("cursor") is not None and not isinstance(state.get("cursor"), str)):
            raise client.ProbeError("stream state is incompatible with current session; do not discard it silently")
        return state
    state = {"meeting_id": meeting_id, "query_start": int(now) - initial_lookback,
             "cursor": None, "cursor_recent": [], "watermark": None, "last_caption_time": None, "seen": {}}
    _write_state(path, state)
    return state


def _append_lines(path, rows, max_bytes):
    if not rows:
        return
    content = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows).encode("utf-8")
    size = path.stat().st_size if path.exists() else 0
    if size + len(content) > max_bytes:
        raise client.ProbeError("stream log size limit reached; stop and archive/delete logs locally")
    if path.is_symlink():
        raise client.ProbeError("log must not be a symlink")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "ab") as file:
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
    finally:
        # fdopen owns fd; an exception before fdopen is unlikely but close defensively.
        try:
            os.close(fd)
        except OSError:
            pass


def _event_time(event, item=None):
    value = event.get("event_time")
    if isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if moment.tzinfo is not None:
                return int(moment.timestamp())
        except ValueError:
            pass
    if item is not None:
        value = item.get("end_time_ms") or item.get("start_time_ms")
        if isinstance(value, (int, str)) and str(value).isascii() and str(value).isdecimal():
            return int(value) // 1000
    return None


def _page_records(events, state, *, received_at, max_seen):
    """Do not keep original payloads beyond this page. Updates are tentative until log commit."""
    seen = dict(state["seen"])
    records = []
    kinds = Counter()
    last_time = state.get("watermark")
    fallback_keys = 0
    skipped_duplicates = 0
    unresolved_revisions = 0
    for event in events:
        kind, payload = stage0.activity(event)
        kinds[kind if kind in ("transcript_received", "participant_joined", "participant_left", "chat_received") else "other"] += 1
        timestamp = _event_time(event)
        if timestamp is not None:
            last_time = max(last_time or timestamp, timestamp)
        if kind != "transcript_received":
            continue
        for index, item in enumerate(stage0.transcript_items(payload)):
            if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                raise client.ProbeError("invalid transcript item (text missing); page not committed")
            sid = item.get("sentence_id")
            if sid is not None and not isinstance(sid, str):
                raise client.ProbeError("invalid sentence_id; page not committed")
            eid = event.get("event_id")
            speaker = item.get("speaker")
            speaker_name = speaker.get("user_name") if isinstance(speaker, dict) else None
            speaker_id = speaker.get("id") if isinstance(speaker, dict) else None
            speaker_key = hashlib.sha256(speaker_id.encode("utf-8")).hexdigest()[:16] if isinstance(speaker_id, str) else None
            if sid:
                key = "s:" + sid
            elif isinstance(eid, str) and eid:
                key = f"e:{eid}:{index}"
                fallback_keys += 1
            else:
                key = "h:" + hashlib.sha256(json.dumps(
                    [item.get("text"), speaker_name, item.get("start_time_ms"), item.get("end_time_ms")],
                    ensure_ascii=False).encode()).hexdigest()
                fallback_keys += 1
            text_hash = hashlib.sha256(json.dumps([item["text"], speaker_key, item.get("language")],
                                                  ensure_ascii=False).encode("utf-8")).hexdigest()
            entry_time = _event_time(event, item)
            old = seen.get(key)
            if old is not None:
                if old["hash"] == text_hash:
                    skipped_duplicates += 1
                    continue
                # An older page can replay a pre-revision version. Do not regress to it.
                if entry_time is None or old["version"] is None or entry_time <= old["version"]:
                    unresolved_revisions += 1
                    continue
            revision = old is not None
            seen.pop(key, None)
            seen[key] = {"hash": text_hash, "version": entry_time}
            if entry_time is not None:
                last_time = max(last_time or entry_time, entry_time)
            records.append({"text": item["text"], "speaker": speaker_name,
                            "speaker_key": speaker_key, "language": item.get("language"), "sentence_id": sid,
                            "start_time_ms": item.get("start_time_ms"), "end_time_ms": item.get("end_time_ms"),
                            "event_id": eid if isinstance(eid, str) else None,
                            "event_time": event.get("event_time"), "received_at": received_at,
                            "revision": revision})
    while len(seen) > max_seen:
        seen.pop(next(iter(seen)))
    return records, seen, last_time, dict(kinds), fallback_keys, skipped_duplicates, unresolved_revisions


class Stream:
    def __init__(self, config, *, api=client.call, base=BASE, clock=time.time, monotonic=time.monotonic, sleep=time.sleep):
        self.config = config
        self.api = api
        self.clock = clock
        self.monotonic = monotonic
        self.sleep = sleep
        self.settings = options(config)
        self.meeting_id = bot_join.load_session(config)
        # No meeting number or ID in path names; state is isolated per join response ID.
        self.directory = base / ("meeting_" + hashlib.sha256(self.meeting_id.encode()).hexdigest()[:16])
        _private_dir(base)
        _private_dir(self.directory)
        self._lock_fd = os.open(self.directory / "stream.lock", os.O_WRONLY | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        os.fchmod(self._lock_fd, 0o600)
        try:
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self._lock_fd)
            raise client.ProbeError("another streamer is running for this meeting") from None
        try:
            self.state_path = self.directory / "state.json"
            self.state = _load_state(self.state_path, self.meeting_id, now=clock(),
                                     initial_lookback=self.settings["initial_lookback_seconds"])
            run_dir = self.directory / ("run_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "_" + str(os.getpid()))
            _private_dir(run_dir)
            self.transcript_log = run_dir / "transcripts.jsonl"
            self.diag_log = run_dir / "diagnostics.jsonl"
            self.failed_page_log = run_dir / "failed_pages.jsonl"
            self.token = client.tenant_token(config, api=api)
            self.token_at = monotonic()
            self.last_call = None
        except Exception:
            self.close()
            raise

    def close(self):
        if self._lock_fd is not None:
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            os.close(self._lock_fd)
            self._lock_fd = None

    def _rate_limit(self):
        now = self.monotonic()
        if self.last_call is not None:
            delay = self.settings["request_seconds"] - (now - self.last_call)
            if delay > 0:
                self.sleep(delay)
        self.last_call = self.monotonic()

    def cycle(self):
        """Checkpoint and yield each page of normalized captions before requesting more."""
        for _ in range(self.settings["max_pages_per_cycle"]):
            if self.monotonic() - self.token_at > 5400:  # proactive refresh before 2h expiry
                self.token = client.tenant_token(self.config, api=self.api)
                self.token_at = self.monotonic()
            self._rate_limit()
            query = {"meeting_id": self.meeting_id, "page_size": self.settings["page_size"],
                     "start_time": str(self.state["query_start"])}
            cursor = self.state["cursor"]
            if cursor:
                query["page_token"] = cursor
            start = self.monotonic()
            response = self.api(stage0.EVENTS, token=self.token, query=query, timeout=client.timeout(self.config))
            elapsed_ms = round((self.monotonic() - start) * 1000)
            data = response.get("data")
            if not isinstance(data, dict) or not isinstance(data.get("events"), list) or not isinstance(data.get("has_more"), bool):
                raise client.ProbeError("events response missing data.events or has_more; page not committed")
            nxt = data.get("page_token")
            if data["has_more"] and (not isinstance(nxt, str) or not nxt or nxt == cursor):
                raise client.ProbeError("events has_more without a new page_token; page not committed")
            recent = self.state.get("cursor_recent", [])
            if data["has_more"] and nxt in recent:
                raise client.ProbeError("events page_token loop detected; page not committed")
            events = data["events"]
            received_at = datetime.now(timezone.utc).isoformat()
            try:
                records, seen, watermark, kinds, fallbacks, duplicates, conflicts = _page_records(
                    events, self.state, received_at=received_at, max_seen=self.settings["max_seen"])
            except client.ProbeError:
                if self.settings["capture_failed_page"]:
                    # Explicit opt-in only: raw events can contain meeting content/participants.
                    _append_lines(self.failed_page_log, [{"at": received_at,
                        "cursor_hash": hashlib.sha256(cursor.encode()).hexdigest()[:12] if cursor else None,
                        "events": events}], self.settings["max_log_bytes"])
                raise
            caption_time = self.state.get("last_caption_time")
            for row in records:
                stamp = _event_time({"event_time": row["event_time"]}, row)
                if stamp is not None:
                    caption_time = max(caption_time or stamp, stamp)
            diag = {"at": received_at, "kind": "page", "event_count": len(events), "kinds": kinds,
                    "new_sentences": len(records), "revision_count": sum(r["revision"] for r in records),
                    "dedup_fallbacks": fallbacks, "skipped_duplicates": duplicates,
                    "unresolved_revisions": conflicts, "latency_ms": elapsed_ms,
                    "cursor_present": bool(cursor),
                    "cursor_hash": hashlib.sha256(cursor.encode()).hexdigest()[:12] if cursor else None,
                    "has_more": data["has_more"], "query_start": self.state["query_start"],
                    "watermark_present": watermark is not None,
                    "caption_age_seconds": max(0, int(self.clock()) - caption_time) if caption_time is not None else None}
            # A flushed subtitle log precedes the state checkpoint. On a crash between
            # writes, a repeated record is possible, but an acknowledged record isn't lost.
            _append_lines(self.transcript_log, records, self.settings["max_log_bytes"])
            _append_lines(self.diag_log, [diag], self.settings["max_log_bytes"])
            self.state["seen"] = seen
            self.state["watermark"] = watermark
            self.state["last_caption_time"] = caption_time
            self.state["cursor"] = nxt if data["has_more"] else None
            self.state["cursor_recent"] = (recent + [nxt])[-100:] if data["has_more"] else []
            if not data["has_more"] and watermark is not None:
                self.state["query_start"] = max(self.state["query_start"], watermark - self.settings["overlap_seconds"])
            _write_state(self.state_path, self.state)
            # Release original payload references; the caller holds only normalized sentences.
            del events, data, response
            yield records
            if not self.state["cursor"]:
                break

    def run(self):
        print("Streaming subtitles. Sensitive text is logged locally under v2/output/. Ctrl-C stops polling, not the bot.", file=sys.stderr)
        failures = 0
        while True:
            try:
                for records in self.cycle():
                    for item in records:
                        prefix = "[修订] " if item["revision"] else ""
                        print(prefix + stage0.subtitle_line(item), flush=True)
                failures = 0
                self.sleep(self.settings["poll_seconds"])
            except KeyboardInterrupt:
                print("Stopped polling; bot remains in meeting.", file=sys.stderr)
                return
            except client.ProbeError as exc:
                failures += 1
                msg = str(exc)  # feishu_client hides server msg and tokens in normal mode
                _append_lines(self.diag_log, [{"at": datetime.now(timezone.utc).isoformat(),
                    "kind": "error", "message": msg[:300], "failures": failures}], self.settings["max_log_bytes"])
                if failures >= 5 or not any(marker in msg for marker in ("HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504", "network or invalid JSON")):
                    raise
                self.sleep(min(30, 2 ** failures))


def main():
    try:
        stream = Stream(client.load_config())
        try:
            stream.run()
        finally:
            stream.close()
    except client.ProbeError as exc:
        print("Stream stopped: " + str(exc), file=sys.stderr)
        return 1
    except OSError:
        print("Stream stopped: local file error; check permissions and free space under v2/output/.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
