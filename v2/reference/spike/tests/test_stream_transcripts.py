import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch
from contextlib import redirect_stderr
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bot_join
import feishu_client as client
import stage0
import stream_transcripts as stream

MID = "987654321012345678"


def event(sid, text, epoch, *, kind="transcript_received"):
    return {"event_id": f"evt-{sid}-{epoch}", "event_type": kind,
            "event_time": datetime.fromtimestamp(epoch, timezone.utc).isoformat(),
            "payload": {"transcript_received_items": [{"sentence_id": sid, "text": text,
                "speaker": {"user_name": "同事"}, "language": "zh",
                "start_time_ms": str(epoch * 1000), "end_time_ms": str(epoch * 1000 + 500)}]}}


class StreamTests(unittest.TestCase):
    def setUp(self):
        self.config = {"app": {"id": "app", "secret": "secret"},
                       "lookup": {"meeting_no": "012345678"},
                       "probe": {"timeout_seconds": 5},
                       "stream": {"initial_lookback_seconds": 300, "overlap_seconds": 120,
                                  "max_pages_per_cycle": 3, "poll_seconds": 2}}
        self.now = [1_800_000_000.0]

    def clock(self):
        return self.now[0]

    def sleep(self, seconds):
        self.now[0] += seconds

    def test_page_checkpoint_resume_dedup_and_no_full_payload_in_diagnostics(self):
        base_time = 1_800_000_000
        calls = []
        def api(path, **kw):
            calls.append((path, kw))
            if path == client.TENANT_TOKEN:
                return {"tenant_access_token": "token"}
            if "page_token" not in kw["query"]:
                return {"data": {"events": [event("s1", "第一句", base_time)],
                                 "has_more": True, "page_token": "cursor"}}
            return {"data": {"events": [event("s1", "第一句", base_time), event("s2", "第二句", base_time + 2)],
                             "has_more": False}}
        with tempfile.TemporaryDirectory() as d, patch.object(bot_join, "load_session", return_value=MID):
            s = stream.Stream(self.config, api=api, base=Path(d)/"output", clock=self.clock,
                              monotonic=self.clock, sleep=self.sleep)
            pages = list(s.cycle())
            self.assertEqual([[r["text"] for r in page] for page in pages], [["第一句"], ["第二句"]])
            self.assertEqual(calls[1][1]["query"]["start_time"], str(base_time - 300))
            self.assertEqual(calls[1][1]["query"]["page_size"], 100)
            self.assertEqual(calls[2][1]["query"]["page_token"], "cursor")
            state = json.loads(s.state_path.read_text())
            self.assertIsNone(state["cursor"])
            self.assertEqual(state["query_start"], base_time - 118)
            self.assertEqual(len(state["seen"]), 2)
            self.assertEqual(stat.S_IMODE(s.state_path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(s.directory.stat().st_mode), 0o700)
            transcripts = s.transcript_log.read_text()
            diagnostics = s.diag_log.read_text()
            self.assertEqual(len(transcripts.splitlines()), 2)
            self.assertNotIn("第一句", diagnostics)
            self.assertNotIn("token", diagnostics)
            self.assertEqual(json.loads(diagnostics.splitlines()[1])["caption_age_seconds"], 0)
            # Concurrent writers for the same meeting are rejected; restart restores state.
            with self.assertRaisesRegex(client.ProbeError, "another streamer"):
                stream.Stream(self.config, api=api, base=Path(d)/"output", clock=self.clock,
                              monotonic=self.clock, sleep=self.sleep)
            s.close()
            s2 = stream.Stream(self.config, api=api, base=Path(d)/"output", clock=self.clock,
                               monotonic=self.clock, sleep=self.sleep)
            self.assertEqual([len(rows) for rows in s2.cycle()], [0, 0])
            s2.close()

    def test_checkpoint_after_first_page_survives_later_page_failure(self):
        def api(path, **kw):
            if path == client.TENANT_TOKEN:
                return {"tenant_access_token": "token"}
            if not kw["query"].get("page_token"):
                return {"data": {"has_more": True, "page_token": "next",
                    "events": [event("s1", "已经记录", 1_800_000_000)]}}
            raise client.ProbeError("HTTP 503; msg hidden")
        with tempfile.TemporaryDirectory() as d, patch.object(bot_join, "load_session", return_value=MID):
            s = stream.Stream(self.config, api=api, base=Path(d)/"output", clock=self.clock,
                              monotonic=self.clock, sleep=self.sleep)
            gen = s.cycle()
            self.assertEqual(next(gen)[0]["text"], "已经记录")
            with self.assertRaises(client.ProbeError):
                next(gen)
            self.assertEqual(json.loads(s.state_path.read_text())["cursor"], "next")
            self.assertIn("已经记录", s.transcript_log.read_text())

    def test_replayed_older_text_cannot_undo_newer_revision(self):
        state = {"seen": {}, "watermark": None}
        one, seen, watermark, *_ = stream._page_records([event("s1", "原文", 1_800_000_000)], state,
                received_at="test", max_seen=100)
        self.assertEqual(len(one), 1)
        two, seen, watermark, *_ = stream._page_records([event("s1", "修订文", 1_800_000_002)],
                {"seen": seen, "watermark": watermark}, received_at="test", max_seen=100)
        self.assertTrue(two[0]["revision"])
        old, seen, _, _, _, duplicates, conflicts = stream._page_records(
                [event("s1", "原文", 1_800_000_000)], {"seen": seen, "watermark": watermark},
                received_at="test", max_seen=100)
        self.assertEqual(old, [])
        self.assertEqual(conflicts, 1)
        self.assertEqual(seen["s:s1"]["hash"], hashlib.sha256(
            json.dumps(["修订文", None, "zh"], ensure_ascii=False).encode()).hexdigest())

    def test_empty_page_is_checkpointed_without_advancing_unknown_watermark(self):
        def api(path, **kwargs):
            if path == client.TENANT_TOKEN:
                return {"tenant_access_token": "token"}
            return {"data": {"events": [], "has_more": False}}
        with tempfile.TemporaryDirectory() as d, patch.object(bot_join, "load_session", return_value=MID):
            s = stream.Stream(self.config, api=api, base=Path(d)/"output", clock=self.clock,
                              monotonic=self.clock, sleep=self.sleep)
            self.assertEqual(list(s.cycle()), [[]])
            state = json.loads(s.state_path.read_text())
            self.assertEqual(state["query_start"], 1_800_000_000 - 300)
            self.assertIsNone(state["cursor"])
            self.assertFalse(s.transcript_log.exists())
            self.assertEqual(json.loads(s.diag_log.read_text().splitlines()[0])["event_count"], 0)
            s.close()

    def test_log_quota_failure_does_not_advance_cursor(self):
        self.config["stream"]["max_log_bytes"] = 10_000
        def api(path, **kwargs):
            if path == client.TENANT_TOKEN:
                return {"tenant_access_token": "token"}
            return {"data": {"events": [event("s1", "第一句", 1_800_000_000)], "has_more": False}}
        with tempfile.TemporaryDirectory() as d, patch.object(bot_join, "load_session", return_value=MID):
            s = stream.Stream(self.config, api=api, base=Path(d)/"output", clock=self.clock,
                              monotonic=self.clock, sleep=self.sleep)
            s.transcript_log.write_bytes(b"x" * 10_000)
            with self.assertRaisesRegex(client.ProbeError, "log size limit"):
                list(s.cycle())
            self.assertEqual(json.loads(s.state_path.read_text())["seen"], {})
            s.close()

    def test_repeated_page_token_cycle_stops_instead_of_looping_forever(self):
        def api(path, **kwargs):
            if path == client.TENANT_TOKEN:
                return {"tenant_access_token": "token"}
            current = kwargs["query"].get("page_token")
            return {"data": {"events": [], "has_more": True, "page_token": "B" if current == "A" else "A"}}
        self.config["stream"]["max_pages_per_cycle"] = 1
        with tempfile.TemporaryDirectory() as d, patch.object(bot_join, "load_session", return_value=MID):
            s = stream.Stream(self.config, api=api, base=Path(d)/"output", clock=self.clock,
                              monotonic=self.clock, sleep=self.sleep)
            self.assertEqual(list(s.cycle()), [[]])
            self.assertEqual(list(s.cycle()), [[]])
            with self.assertRaisesRegex(client.ProbeError, "loop detected"):
                list(s.cycle())
            s.close()

    def test_raw_failed_page_only_with_explicit_opt_in(self):
        def api(path, **kwargs):
            if path == client.TENANT_TOKEN:
                return {"tenant_access_token": "token"}
            return {"data": {"events": [{"event_type": "transcript_received",
                "payload": {"transcript_received_items": [{"speaker": {"user_name": "隐私姓名"},
                    "unexpected": "敏感内容但没有 text"}]}}], "has_more": False}}
        with tempfile.TemporaryDirectory() as d, patch.object(bot_join, "load_session", return_value=MID):
            s = stream.Stream(self.config, api=api, base=Path(d)/"output", clock=self.clock,
                              monotonic=self.clock, sleep=self.sleep)
            with self.assertRaises(client.ProbeError):
                list(s.cycle())
            self.assertFalse(s.failed_page_log.exists())
            s.close()
            self.config["stream"]["capture_failed_page"] = True
            other = stream.Stream(self.config, api=api, base=Path(d)/"output", clock=self.clock,
                                  monotonic=self.clock, sleep=self.sleep)
            with self.assertRaises(client.ProbeError):
                list(other.cycle())
            self.assertIn("敏感内容", other.failed_page_log.read_text())
            self.assertEqual(stat.S_IMODE(other.failed_page_log.stat().st_mode), 0o600)
            self.assertEqual(json.loads(other.state_path.read_text())["seen"], {})
            other.close()

    def test_transient_failure_has_bounded_retries_and_safe_error_log(self):
        attempts = [0]
        def api(path, **kwargs):
            if path == client.TENANT_TOKEN:
                return {"tenant_access_token": "token"}
            attempts[0] += 1
            raise client.ProbeError("HTTP 503 (/open-apis/vc/v1/bots/events); response hidden")
        with tempfile.TemporaryDirectory() as d, patch.object(bot_join, "load_session", return_value=MID):
            s = stream.Stream(self.config, api=api, base=Path(d)/"output", clock=self.clock,
                              monotonic=self.clock, sleep=self.sleep)
            with redirect_stderr(io.StringIO()), self.assertRaises(client.ProbeError):
                s.run()
            self.assertEqual(attempts[0], 5)
            self.assertEqual(len(s.diag_log.read_text().splitlines()), 5)
            self.assertEqual(json.loads(s.state_path.read_text())["seen"], {})
            s.close()

    def test_options_and_bad_data_fail_closed(self):
        self.config["stream"]["request_seconds"] = 0.1
        with self.assertRaises(client.ProbeError):
            stream.options(self.config)
        with self.assertRaises(client.ProbeError):
            stream._page_records([{"event_type": "transcript_received", "payload": {
                "transcript_received_items": [{"sentence_id": "s1"}]}}],
                {"seen": {}, "watermark": None}, received_at="test", max_seen=100)


if __name__ == "__main__":
    unittest.main()
