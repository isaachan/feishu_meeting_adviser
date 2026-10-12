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

    def test_http_error_hides_response(self):
        def deny(*args, **kwargs):
            raise error.HTTPError("url", 403, "PRIVATE BODY", {}, io.BytesIO(b"PRIVATE BODY"))
        with self.assertRaises(stage0.ProbeError) as raised:
            stage0.call(stage0.EVENTS, token="secret", opener=deny)
        self.assertNotIn("PRIVATE BODY", str(raised.exception))
        self.assertNotIn("secret", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
