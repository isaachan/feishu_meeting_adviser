"""Join the app bot, inspect events or explicitly display meeting subtitles.

Run: python3 v2/scripts/stage0.py join  # then: probe, transcripts or diagnose
User-provided inputs come only from v2/config.yaml. The join response meeting ID
is stored locally in ignored v2/session.yaml. `transcripts` prints sensitive text.
"""

import argparse
import json
import re
import sys

from feishu_client import CONFIG, TENANT_TOKEN, ProbeError, call, load_config, tenant_token, timeout
import bot_join

EVENTS = "/open-apis/vc/v1/bots/events"
_SAFE_EVENT_TYPE = re.compile(r"[A-Za-z][A-Za-z0-9_.:-]{0,63}\Z")


def safe_event_type(value):
    """Only expose short enum-like identifiers, never arbitrary meeting content."""
    return value if isinstance(value, str) and _SAFE_EVENT_TYPE.fullmatch(value) else "unrecognized_or_missing"


def activity(event):
    if not isinstance(event, dict):
        raise ProbeError("event is not an object")
    event_type = event.get("event_type")
    # Real meeting response observed by the user uses event_type=transcript_received
    # directly; the public docs also show meeting.activity + activity_event_type.
    if event_type not in ("meeting.activity", "transcript_received", "chat_received"):
        return None, {}
    payload = event.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except ValueError:
            raise ProbeError("invalid meeting.activity payload JSON; content hidden") from None
    if not isinstance(payload, dict):
        raise ProbeError("meeting.activity payload is not an object")
    return (payload.get("activity_event_type") if event_type == "meeting.activity" else event_type), payload


def event_summary(events):
    """Only schema metadata; never return text, IDs, timestamps or speaker identity."""
    if not isinstance(events, list):
        raise ProbeError("events field is not a list; inspect official contract privately")
    types = {}
    transcript_schema = {}
    for event in events:
        kind, payload = activity(event)
        label = kind if kind in ("transcript_received", "chat_received") else "other_or_missing"
        types[label] = types.get(label, 0) + 1
        if kind == "transcript_received":
            items = transcript_items(payload)
            transcript_schema = {"transcript_received_items": type(items).__name__}
            if isinstance(items, list) and items and isinstance(items[0], dict):
                keys = ("speaker", "text", "language", "start_time_ms", "end_time_ms", "sentence_id")
                transcript_schema.update({k: type(items[0][k]).__name__ for k in keys if k in items[0]})
    return {"counts": types, "transcript_field_types": transcript_schema}


def transcript_items(payload):
    """Handle documented item list and directly embedded single-sentence payloads."""
    items = payload.get("transcript_received_items")
    if isinstance(items, list):
        return items
    if isinstance(payload.get("text"), str):
        return [payload]
    # No arbitrary key/value text leaves this process; field *names* come from a whitelist.
    known = ("transcript_received_items", "text", "speaker", "sentence_id", "data", "items", "transcript")
    shape = {key: type(payload[key]).__name__ for key in known if key in payload}
    raise ProbeError("unrecognized transcript payload shape " + json.dumps(shape) + "; no partial subtitles printed")


def event_time_query(config):
    params = {}
    for key in ("start_time", "end_time"):
        value = config["probe"].get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (str, int)) or not str(value).isascii() or not str(value).isdecimal() or len(str(value)) != 10:
            raise ProbeError(f"probe.{key} must be a 10-digit Unix-seconds timestamp")
        params[key] = str(value)
    if "start_time" in params and "end_time" in params and int(params["start_time"]) > int(params["end_time"]):
        raise ProbeError("probe.start_time must not be after probe.end_time")
    return params


def fetch_pages(config, *, api=call, session_path=bot_join.SESSION):
    """Single bounded fetch, not a persistent cursor or live poller."""
    meeting_id = bot_join.load_session(config, session_path)
    limit = config["probe"].get("max_pages", 3)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
        raise ProbeError("probe.max_pages must be 1..10")
    t = timeout(config)
    time_params = event_time_query(config)
    token = tenant_token(config, api=api)
    pages = []
    cursor = None
    seen = set()
    for _ in range(limit):
        params = {"meeting_id": meeting_id, **time_params}
        if cursor:
            params["page_token"] = cursor
        response = api(EVENTS, token=token, query=params, timeout=t)
        data = response.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("events"), list):
            raise ProbeError("events response lacks data.events list; inspect contract privately")
        more = data.get("has_more")
        if not isinstance(more, bool):
            raise ProbeError("events response missing has_more")
        pages.append(data["events"])
        if not more:
            return meeting_id, pages, False
        nxt = data.get("page_token")
        if not isinstance(nxt, str) or not nxt or nxt == cursor or nxt in seen:
            raise ProbeError("events has_more but no new page_token; refusing partial results")
        seen.add(nxt)
        cursor = nxt
    return meeting_id, pages, True


def probe(config, *, api=call, session_path=bot_join.SESSION):
    meeting_id, pages, more = fetch_pages(config, api=api, session_path=session_path)
    summary = {"meeting_id_digits": len(meeting_id), "pages": len(pages),
               "event_counts": {}, "transcript_field_types": {}, "more_pages": more}
    for events in pages:
        stats = event_summary(events)
        for key, count in stats["counts"].items():
            summary["event_counts"][key] = summary["event_counts"].get(key, 0) + count
        if stats["transcript_field_types"]:
            summary["transcript_field_types"] = stats["transcript_field_types"]
    return summary


def transcripts(config, *, api=call, session_path=bot_join.SESSION, diagnostics=None):
    """Return fetched sentences; optional diagnostics contain counts only, never meeting text."""
    _meeting_id, pages, more = fetch_pages(config, api=api, session_path=session_path)
    if more:
        raise ProbeError("more event pages than probe.max_pages; increase up to 10 or narrow scope; no partial subtitles printed")
    records = []
    seen_sentences = set()
    if diagnostics is not None:
        diagnostics.update(pages=len(pages), events=0, transcript_events=0,
                           transcript_items=0, time_filter_active=bool(event_time_query(config)),
                           event_types={}, activity_types={}, event_shapes={})
    for events in pages:
        if diagnostics is not None:
            diagnostics["events"] += len(events)
        for event in events:
            kind, payload = activity(event)
            if diagnostics is not None:
                event_label = safe_event_type(event.get("event_type"))
                types = diagnostics["event_types"]
                types[event_label] = types.get(event_label, 0) + 1
                shape = diagnostics["event_shapes"]
                for key in ("event_type", "type", "payload", "data", "activity_event_type", "transcript_received_items"):
                    if key in event:
                        label = key + ":" + type(event[key]).__name__
                        shape[label] = shape.get(label, 0) + 1
                if event_label == "meeting.activity":
                    allowed = ("transcript_received", "participant_joined", "participant_left",
                               "chat_received", "magic_share_started", "magic_share_ended")
                    subtype = kind if kind in allowed else "other_or_missing"
                    types = diagnostics["activity_types"]
                    types[subtype] = types.get(subtype, 0) + 1
            if kind != "transcript_received":
                continue
            if diagnostics is not None:
                diagnostics["transcript_events"] += 1
            items = transcript_items(payload)
            if diagnostics is not None:
                diagnostics["transcript_items"] += len(items)
            for item in items:
                if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                    raise ProbeError("transcript item has no text string; no partial subtitles printed")
                sid = item.get("sentence_id")
                if sid is not None and not isinstance(sid, str):
                    raise ProbeError("invalid sentence_id type; no partial subtitles printed")
                if sid and sid in seen_sentences:
                    continue
                if sid:
                    seen_sentences.add(sid)
                speaker = item.get("speaker")
                records.append({"speaker": speaker.get("user_name") if isinstance(speaker, dict) else None,
                                "text": item["text"], "language": item.get("language"),
                                "start_time_ms": item.get("start_time_ms"),
                                "end_time_ms": item.get("end_time_ms"), "sentence_id": sid})
    return records


def subtitle_line(row):
    """One subtitle per terminal line, preserving text while folding line breaks."""
    return row["text"].replace("\r", " ").replace("\n", " ")


def main():
    parser = argparse.ArgumentParser(description="Feishu bot join and event probe; inputs only in v2/config.yaml")
    parser.add_argument("command", choices=("join", "probe", "transcripts", "diagnose"))
    args = parser.parse_args()
    try:
        config = load_config()
        if args.command == "join":
            bot_join.join_and_save(config)
            print("Bot joined; meeting ID saved to local ignored v2/session.yaml (not printed).")
        elif args.command == "probe":
            print(json.dumps(probe(config), ensure_ascii=False, indent=2))
        elif args.command == "diagnose":
            def diagnostic_api(path, **kwargs):
                if path != EVENTS:
                    return call(path, **kwargs)
                query = kwargs.get("query") or {}
                print("Local-only request shape: " + json.dumps({
                    "method": "GET", "path": EVENTS, "query_keys": sorted(query),
                    "meeting_id_digits": len(query.get("meeting_id", "")),
                    "page_token_present": bool(query.get("page_token")),
                    "auth": "tenant_access_token"}, ensure_ascii=False), file=sys.stderr)
                return call(path, diagnose=True, redact_values=(config["app"].get("id"),
                            config["app"].get("secret"), config["lookup"].get("meeting_no")), **kwargs)
            print(json.dumps(probe(config, api=diagnostic_api), ensure_ascii=False, indent=2))
        else:
            diagnostic = {}
            rows = transcripts(config, diagnostics=diagnostic)
            if not rows:
                if diagnostic["transcript_events"] == 0:
                    reason = "No transcript_received event in these pages; inspect safe event/activity counts, AI summary setting and meeting session."
                else:
                    reason = "transcript_received returned zero items; verify upstream caption generation."
                print("No transcript sentences in this fetch; safe diagnostics: "
                      + json.dumps(diagnostic, ensure_ascii=False) + ". " + reason, file=sys.stderr)
            for row in rows:
                print(subtitle_line(row))
    except ProbeError as exc:
        print(f"Stage 0 failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
