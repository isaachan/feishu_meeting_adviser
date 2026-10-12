"""Find internal meeting IDs from a 9-digit meeting number via app identity.

Inputs are ONLY read from v2/config.yaml; this tool never edits the config.
Run: python3 v2/scripts/lookup_meeting_id.py
"""

import json
import sys
import time
from urllib import parse

import feishu_client as client

LIST_BY_NO = "/open-apis/vc/v1/meetings/list_by_no"
MEETING = "/open-apis/vc/v1/meetings/"


def meeting_number(config):
    section = config.get("lookup")
    if not isinstance(section, dict):
        raise client.ProbeError("config.yaml needs a lookup mapping with a 9-digit meeting_no")
    meeting_no = section.get("meeting_no")
    if not isinstance(meeting_no, str) or not meeting_no.isascii() or not meeting_no.isdecimal() or len(meeting_no) != 9:
        raise client.ProbeError("lookup.meeting_no must be a quoted 9-digit string, NOT the internal meeting ID")
    return meeting_no


def time_range(config, now):
    lookup = config["lookup"]
    start = lookup.get("start_time")
    end = lookup.get("end_time")
    if start is None and end is None:
        return now - 24 * 3600, now
    if start is None or end is None:
        raise client.ProbeError("lookup.start_time and lookup.end_time must both be set (Unix seconds)")

    def seconds(value):
        if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).isascii() or not str(value).isdecimal():
            raise client.ProbeError("lookup time range must use Unix seconds")
        return int(value)

    start, end = seconds(start), seconds(end)
    if start >= end or start < now - 90 * 86400 or end > now + 60:
        raise client.ProbeError("lookup time range must be ordered and within the past 90 days")
    return start, end


def lookup(config, *, api=client.call, now_fn=time.time, token=None):
    meeting_no = meeting_number(config)
    section = config["lookup"]
    max_pages = section.get("max_pages", 3)
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or not 1 <= max_pages <= 10:
        raise client.ProbeError("lookup.max_pages must be 1..10")
    start, end = time_range(config, int(now_fn()))
    t = client.timeout(config)
    if token is None:
        token = client.tenant_token(config, api=api)
    ids = []
    seen_ids = set()
    cursor = None
    seen_cursors = set()
    for _ in range(max_pages):
        query = {"meeting_no": meeting_no, "start_time": str(start), "end_time": str(end)}
        if cursor:
            query["page_token"] = cursor
        response = api(LIST_BY_NO, token=token, query=query, timeout=t)
        data = response.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("meeting_briefs"), list):
            raise client.ProbeError("list_by_no response shape differs from documented data.meeting_briefs")
        for brief in data["meeting_briefs"]:
            if not isinstance(brief, dict):
                raise client.ProbeError("unexpected meeting brief shape")
            if brief.get("meeting_no") != meeting_no:
                continue
            mid = brief.get("id")
            if isinstance(mid, str) and mid.isascii() and mid.isdecimal() and len(mid) > 9 and mid not in seen_ids:
                seen_ids.add(mid)
                ids.append(mid)
        more = data.get("has_more")
        if not isinstance(more, bool):
            raise client.ProbeError("list_by_no response missing has_more")
        if not more:
            break
        nxt = data.get("page_token")
        if not isinstance(nxt, str) or not nxt or nxt == cursor or nxt in seen_cursors:
            raise client.ProbeError("list_by_no has_more but no new page_token; refusing partial results")
        seen_cursors.add(nxt)
        cursor = nxt
    else:
        raise client.ProbeError("lookup.max_pages reached; narrow lookup time window to avoid partial results")
    if len(ids) > 20:
        raise client.ProbeError("too many matching sessions; narrow lookup time window")
    results = []
    for mid in ids:
        try:
            response = api(MEETING + parse.quote(mid, safe=""), token=token, timeout=t)
            payload = response.get("data")
            meeting = payload.get("meeting") if isinstance(payload, dict) else None
            status = meeting.get("status") if isinstance(meeting, dict) else None
        except client.ProbeError:
            status = None
        label = {1: "calling", 2: "in_progress", 3: "ended"}.get(status, "unverified")
        results.append({"meeting_id": mid, "status": label})
    return results


def main():
    try:
        results = lookup(client.load_config())
        print(json.dumps({"meetings": results}, ensure_ascii=False, indent=2))
        if not results:
            print("No matching sessions. Check 9-digit meeting number, time range and app permission.", file=sys.stderr)
        elif sum(row["status"] == "in_progress" for row in results) != 1:
            print("No unique confirmed ongoing session; check meeting detail permissions and time range.", file=sys.stderr)
    except client.ProbeError as exc:
        print(f"Lookup failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
