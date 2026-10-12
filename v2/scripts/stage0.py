"""Join the app bot and probe meeting events using v2/config.yaml only.

Run: python3 v2/scripts/stage0.py join  # then: stage0.py probe
The join response meeting ID is stored locally in ignored v2/session.yaml.
"""

import argparse
import json
import sys

from feishu_client import CONFIG, TENANT_TOKEN, ProbeError, call, load_config, tenant_token, timeout
import bot_join

EVENTS = "/open-apis/vc/v1/bots/events"


def event_summary(events):
    """Only schema metadata; never return text, IDs, timestamps or speaker identity."""
    if not isinstance(events, list):
        raise ProbeError("events field is not a list; inspect official contract privately")
    types = {}
    transcript_schema = {}
    for event in events:
        if not isinstance(event, dict):
            raise ProbeError("event is not an object")
        payload = event.get("payload")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                payload = None  # Never echo private payload text.
        if not isinstance(payload, dict):
            payload = {}
        kind = payload.get("activity_event_type") if event.get("event_type") == "meeting.activity" else event.get("event_type")
        label = kind if kind in ("transcript_received", "chat_received") else "other_or_missing"
        types[label] = types.get(label, 0) + 1
        if kind == "transcript_received":
            items = payload.get("transcript_received_items")
            transcript_schema = {"transcript_received_items": type(items).__name__}
            if isinstance(items, list) and items and isinstance(items[0], dict):
                keys = ("speaker", "text", "language", "start_time_ms", "end_time_ms", "sentence_id")
                transcript_schema.update({k: type(items[0][k]).__name__ for k in keys if k in items[0]})
    return {"counts": types, "transcript_field_types": transcript_schema}


def probe(config, *, api=call, session_path=bot_join.SESSION):
    # Only the ID returned by the successful bots/join call is valid here.
    meeting_id = bot_join.load_session(config, session_path)
    limit = config["probe"].get("max_pages", 3)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10:
        raise ProbeError("probe.max_pages must be 1..10")
    t = timeout(config)
    token = tenant_token(config, api=api)
    summary = {"meeting_id_digits": len(meeting_id), "pages": 0,
               "event_counts": {}, "transcript_field_types": {}, "more_pages": False}
    page_token = None
    seen = set()
    for _ in range(limit):
        params = {"meeting_id": meeting_id}
        if page_token:
            params["page_token"] = page_token
        response = api(EVENTS, token=token, query=params, timeout=t)
        result = response.get("data")
        if not isinstance(result, dict):
            raise ProbeError("events response lacks data object; inspect contract privately")
        stats = event_summary(result.get("events"))
        summary["pages"] += 1
        for key, count in stats["counts"].items():
            summary["event_counts"][key] = summary["event_counts"].get(key, 0) + count
        if stats["transcript_field_types"]:
            summary["transcript_field_types"] = stats["transcript_field_types"]
        more = result.get("has_more")
        if not isinstance(more, bool):
            raise ProbeError("events response missing has_more")
        summary["more_pages"] = more
        if not more:
            break
        nxt = result.get("page_token")
        # Single bounded scan, NOT a polling cursor algorithm. Do not retry same cursor.
        if not isinstance(nxt, str) or not nxt or nxt == page_token or nxt in seen:
            raise ProbeError("events has_more but no new page_token; refusing partial results")
        seen.add(nxt)
        page_token = nxt
    return summary


def main():
    parser = argparse.ArgumentParser(description="Feishu bot join and event probe; inputs only in v2/config.yaml")
    parser.add_argument("command", choices=("join", "probe"))
    args = parser.parse_args()
    try:
        config = load_config()
        if args.command == "join":
            bot_join.join_and_save(config)
            print("Bot joined; meeting ID saved to local ignored v2/session.yaml (not printed).")
        else:
            print(json.dumps(probe(config), ensure_ascii=False, indent=2))
    except ProbeError as exc:
        print(f"Stage 0 failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
