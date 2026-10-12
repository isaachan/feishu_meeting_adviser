import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from urllib import error

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import bot_join
import stage0


class Stage0Tests(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "app": {"id": "app", "secret": "secret"},
            "lookup": {"meeting_no": "012345678"},
            "probe": {"timeout_seconds": 5, "max_pages": 2},
        }

    def test_default_config_is_in_v2_root(self):
        self.assertEqual(stage0.CONFIG, Path(__file__).resolve().parents[1] / "config.yaml")

    def test_join_then_probe_reuses_join_response_id_without_lookup(self):
        calls = []
        def api(path, **kwargs):
            calls.append((path, kwargs))
            if path == stage0.TENANT_TOKEN:
                return {"tenant_access_token": "tenant-token"}
            if path == bot_join.JOIN:
                return {"data": {"meeting": {"id": "987654321012345678", "meeting_no": "012345678"}}}
            self.assertEqual(path, stage0.EVENTS)
            return {"data": {"events": [
                {"event_type": "meeting.activity", "payload": {"activity_event_type": "transcript_received", "transcript_received_items": [{"text": "PRIVATE SENTENCE"}]}},
                {"event_type": "join"}], "has_more": False}}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "session.yaml"
            bot_join.join_and_save(self.cfg, api=api, path=path, now_fn=lambda: 1000)
            result = stage0.probe(self.cfg, api=api, session_path=path)
            self.assertEqual(bot_join.load_session(self.cfg, path), "987654321012345678")
            self.assertEqual(result["event_counts"]["transcript_received"], 1)
            self.assertEqual([p for p, _ in calls], [stage0.TENANT_TOKEN, bot_join.JOIN, stage0.TENANT_TOKEN, stage0.EVENTS])
            self.assertEqual(calls[1][1]["payload"], {"join_type": 1, "join_identify": {"meeting_no": "012345678"}})
            self.assertEqual(calls[-1][1]["query"], {"meeting_id": "987654321012345678"})
            for secret in ("PRIVATE SENTENCE", "987654321012345678", "tenant-token"):
                self.assertNotIn(secret, json.dumps(result))

    def test_no_session_or_wrong_number_refuses_to_call_events(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "session.yaml"
            with self.assertRaises(stage0.ProbeError):
                stage0.probe(self.cfg, api=lambda *a, **kw: self.fail("network must not run"), session_path=path)
            bot_join.save_session({"meeting_no": "123456789", "meeting_id": "987654321012345678"}, path)
            with self.assertRaises(stage0.ProbeError):
                stage0.probe(self.cfg, api=lambda *a, **kw: self.fail("network must not run"), session_path=path)

    def test_failed_join_invalidates_old_session_without_leaking_secret(self):
        self.cfg["join"] = {"password": "private-password"}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "session.yaml"
            bot_join.save_session({"meeting_no": "012345678", "meeting_id": "987654321012345678"}, path)
            def api(p, **kwargs):
                if p == stage0.TENANT_TOKEN:
                    return {"tenant_access_token": "tenant-token"}
                self.assertEqual(kwargs["payload"]["password"], "private-password")
                raise stage0.ProbeError("Feishu API error code=121016; msg hidden")
            with self.assertRaises(stage0.ProbeError) as raised:
                bot_join.join_and_save(self.cfg, api=api, path=path)
            self.assertFalse(path.exists())
            self.assertNotIn("private-password", str(raised.exception))

    def test_serialized_transcript_payload_and_cursor(self):
        encoded = json.dumps({"activity_event_type": "transcript_received",
                              "transcript_received_items": [{"text": "private", "sentence_id": "private-id"}]})
        summary = stage0.event_summary([{"event_type": "meeting.activity", "payload": encoded}])
        self.assertEqual(summary["counts"], {"transcript_received": 1})
        self.assertEqual(summary["transcript_field_types"]["sentence_id"], "str")
        self.assertNotIn("private", json.dumps(summary))

    def test_direct_transcript_received_matches_real_event_type(self):
        direct = {"event_type": "transcript_received", "payload": {
            "transcript_received_items": [{"text": "测试字幕", "sentence_id": "s1", "speaker": {"user_name": "张三"}}]}}
        self.assertEqual(stage0.event_summary([direct])["counts"], {"transcript_received": 1})
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "session.yaml"
            bot_join.save_session({"meeting_no": "012345678", "meeting_id": "987654321012345678"}, path)
            def api(endpoint, **kwargs):
                if endpoint == stage0.TENANT_TOKEN:
                    return {"tenant_access_token": "tenant-token"}
                return {"data": {"events": [direct], "has_more": False}}
            stats = {}
            rows = stage0.transcripts(self.cfg, api=api, session_path=path, diagnostics=stats)
            self.assertEqual([row["text"] for row in rows], ["测试字幕"])
            self.assertEqual(rows[0]["speaker"], "张三")
            self.assertEqual(stage0.subtitle_line(rows[0]), "测试字幕")
            self.assertEqual(stage0.subtitle_line({"text": "第一行\n第二行"}), "第一行 第二行")
            self.assertEqual(stats["transcript_events"], 1)
            self.assertEqual(stats["transcript_items"], 1)

    def test_direct_single_sentence_payload(self):
        kind, payload = stage0.activity({"event_type": "transcript_received", "payload":
                                         {"speaker": {"user_name": "甲"}, "text": "直接文本", "sentence_id": "s2"}})
        self.assertEqual(kind, "transcript_received")
        self.assertEqual(stage0.transcript_items(payload)[0]["text"], "直接文本")

    def test_transcripts_extracts_text_from_string_and_dict_payload_across_pages(self):
        calls = []
        def api(path, **kwargs):
            calls.append((path, kwargs))
            if path == stage0.TENANT_TOKEN:
                return {"tenant_access_token": "tenant-token"}
            self.assertEqual(path, stage0.EVENTS)
            if "page_token" not in kwargs["query"]:
                return {"data": {"events": [
                    {"event_type": "meeting.activity", "payload": json.dumps({
                        "activity_event_type": "transcript_received", "transcript_received_items": [
                            {"text": "你好 世界", "speaker": {"user_name": "测试发言人"}, "language": "zh",
                             "sentence_id": "s1", "start_time_ms": "1000", "end_time_ms": "2000"}]})}],
                    "has_more": True, "page_token": "next"}}
            return {"data": {"events": [
                {"event_type": "meeting.activity", "payload": {"activity_event_type": "transcript_received",
                    "transcript_received_items": [{"text": "你好 世界", "sentence_id": "s1"},
                                                  {"text": "第二句", "sentence_id": "s2"}]}}],
                "has_more": False}}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "session.yaml"
            bot_join.save_session({"meeting_no": "012345678", "meeting_id": "987654321012345678"}, path)
            self.cfg["probe"]["start_time"] = "1799999999"
            rows = stage0.transcripts(self.cfg, api=api, session_path=path)
        self.assertEqual([row["text"] for row in rows], ["你好 世界", "第二句"])
        self.assertEqual(rows[0]["speaker"], "测试发言人")
        self.assertEqual(calls[-1][1]["query"]["page_token"], "next")
        self.assertEqual(calls[-1][1]["query"]["start_time"], "1799999999")

    def test_empty_subtitles_report_safe_counts_without_content(self):
        diagnostic = {}
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "session.yaml"
            bot_join.save_session({"meeting_no": "012345678", "meeting_id": "987654321012345678"}, path)
            def api(endpoint, **kwargs):
                if endpoint == stage0.TENANT_TOKEN:
                    return {"tenant_access_token": "tenant-token"}
                return {"data": {"events": [
                    {"event_type": "meeting.activity", "payload": {"activity_event_type": "transcript_received",
                        "transcript_received_items": []}},
                    {"event_type": "meeting.activity", "payload": {"activity_event_type": "participant_joined",
                        "participant_joined_items": [{"user_name": "private name"}]}}],
                    "has_more": False}}
            self.assertEqual(stage0.transcripts(self.cfg, api=api, session_path=path, diagnostics=diagnostic), [])
        self.assertEqual(diagnostic, {"pages": 1, "events": 2, "transcript_events": 1,
                                      "transcript_items": 0, "time_filter_active": False,
                                      "event_types": {"meeting.activity": 2},
                                      "activity_types": {"transcript_received": 1, "participant_joined": 1},
                                      "event_shapes": {"event_type:str": 2, "payload:dict": 2}})
        self.assertNotIn("private name", json.dumps(diagnostic))

    def test_safe_event_type_does_not_expose_meeting_text(self):
        self.assertEqual(stage0.safe_event_type("vc.meeting_activity"), "vc.meeting_activity")
        self.assertEqual(stage0.safe_event_type("private meeting content 中文"), "unrecognized_or_missing")
        self.assertEqual(stage0.safe_event_type("X" * 100), "unrecognized_or_missing")

    def test_transcripts_refuses_partial_pages_and_malformed_items(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "session.yaml"
            bot_join.save_session({"meeting_no": "012345678", "meeting_id": "987654321012345678"}, path)
            self.cfg["probe"]["max_pages"] = 1
            def api(endpoint, **kwargs):
                if endpoint == stage0.TENANT_TOKEN:
                    return {"tenant_access_token": "tenant-token"}
                return {"data": {"events": [
                    {"event_type": "meeting.activity", "payload": {"activity_event_type": "transcript_received",
                        "transcript_received_items": [{"text": "private"}]}}],
                    "has_more": True, "page_token": "next"}}
            with self.assertRaisesRegex(stage0.ProbeError, "no partial subtitles"):
                stage0.transcripts(self.cfg, api=api, session_path=path)
            def malformed(endpoint, **kwargs):
                if endpoint == stage0.TENANT_TOKEN:
                    return {"tenant_access_token": "tenant-token"}
                return {"data": {"events": [
                    {"event_type": "meeting.activity", "payload": {"activity_event_type": "transcript_received",
                        "transcript_received_items": [{}]}}], "has_more": False}}
            with self.assertRaisesRegex(stage0.ProbeError, "no text string"):
                stage0.transcripts(self.cfg, api=malformed, session_path=path)

    def test_invalid_time_window(self):
        self.cfg["probe"].update(start_time="1800000100", end_time="1800000000")
        with self.assertRaises(stage0.ProbeError):
            stage0.event_time_query(self.cfg)

    def test_join_password_omitted_when_not_configured(self):
        def api(path, **kwargs):
            if path == bot_join.JOIN:
                self.assertNotIn("password", kwargs["payload"])
                return {"data": {"meeting": {"id": "987654321012345678", "meeting_no": "012345678"}}}
            self.fail("provided token must avoid extra call")
        self.assertEqual(bot_join.join(self.cfg, api=api, token="tenant-token")["meeting_id"], "987654321012345678")

    def test_invalid_join_response_does_not_save_session(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "session.yaml"
            def api(p, **kwargs):
                if p == stage0.TENANT_TOKEN:
                    return {"tenant_access_token": "tenant-token"}
                return {"data": {"meeting": {"id": "987654321012345678", "meeting_no": "wrong"}}}
            with self.assertRaises(stage0.ProbeError):
                bot_join.join_and_save(self.cfg, api=api, path=path)
            self.assertFalse(path.exists())

    def test_legacy_config_not_overwritten(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.yaml"
            original = yaml.safe_dump({**self.cfg, "oauth": {"old_token": "ignored"}})
            path.write_text(original)
            os.chmod(path, 0o600)
            self.assertEqual(stage0.load_config(path)["lookup"]["meeting_no"], "012345678")
            self.assertEqual(path.read_text(), original)

    def test_explicit_diagnose_shows_redacted_msg_and_request_id_only(self):
        class Response(io.BytesIO):
            status = 200
            headers = {"X-Request-Id": "req_abc123"}
        def fake_opener(req, **kwargs):
            self.assertEqual(req.get_method(), "GET")
            return Response(json.dumps({"code": 20001,
                "msg": "invalid request meeting 987654321012345678 token secret-token", "request_id": "req_abc123"}).encode())
        with self.assertRaises(stage0.ProbeError) as caught:
            stage0.call(stage0.EVENTS, token="secret-token", query={"meeting_id": "987654321012345678"},
                        opener=fake_opener, diagnose=True)
        message = str(caught.exception)
        self.assertIn("invalid request", message)
        self.assertIn("req_abc123", message)
        self.assertNotIn("987654321012345678", message)
        self.assertNotIn("secret-token", message)
        with self.assertRaises(stage0.ProbeError) as normal:
            stage0.call(stage0.EVENTS, token="secret-token", opener=fake_opener)
        self.assertIn("msg hidden", str(normal.exception))

    def test_http_error_hides_response(self):
        def deny(*args, **kwargs):
            raise error.HTTPError("url", 403, "PRIVATE BODY", {}, io.BytesIO(b"PRIVATE BODY"))
        with self.assertRaises(stage0.ProbeError) as raised:
            stage0.call(stage0.EVENTS, token="secret", opener=deny)
        self.assertNotIn("PRIVATE BODY", str(raised.exception))
        self.assertNotIn("secret", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
