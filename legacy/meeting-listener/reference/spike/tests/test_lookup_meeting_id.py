import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import lookup_meeting_id as lookup
import stage0

NOW = 1_800_000_000


class LookupTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "app": {"id": "app", "secret": "secret"},
            "probe": {"timeout_seconds": 5},
            "lookup": {"meeting_no": "012345678", "max_pages": 3},
        }

    def test_get_token_paginate_filter_and_verify_status(self):
        calls = []
        def api(path, **kwargs):
            calls.append((path, kwargs))
            if path == stage0.TENANT_TOKEN:
                return {"tenant_access_token": "tenant-secret"}
            if path == lookup.LIST_BY_NO:
                if "page_token" not in kwargs["query"]:
                    return {"data": {"has_more": True, "page_token": "next", "meeting_briefs": [
                        {"meeting_no": "012345678", "id": "987654321012345678"},
                        {"meeting_no": "111111111", "id": "222222222222222222"}]}}
                return {"data": {"has_more": False, "meeting_briefs": [
                    {"meeting_no": "012345678", "id": "111111111012345678"}]}}
            if path.endswith("987654321012345678"):
                return {"data": {"meeting": {"status": 3, "topic": "private"}}}
            return {"data": {"meeting": {"status": 2, "password": "private"}}}
        result = lookup.lookup(self.cfg, api=api, now_fn=lambda: NOW)
        self.assertEqual(result, [
            {"meeting_id": "987654321012345678", "status": "ended"},
            {"meeting_id": "111111111012345678", "status": "in_progress"},
        ])
        self.assertEqual(calls[0][1]["payload"], {"app_id": "app", "app_secret": "secret"})
        self.assertEqual(calls[1][1]["token"], "tenant-secret")
        self.assertEqual(calls[1][1]["query"], {"meeting_no": "012345678",
                                                 "start_time": str(NOW - 86400), "end_time": str(NOW)})
        self.assertEqual(calls[2][1]["query"]["page_token"], "next")
        self.assertNotIn("meeting_id", self.cfg["probe"])
        self.assertNotIn("private", str(result))

    def test_reject_invalid_number_before_network(self):
        for value in ("12345678", "1234567890", "12a456789", 123456789, ""):
            self.cfg["lookup"]["meeting_no"] = value
            with self.subTest(value=value), self.assertRaises(stage0.ProbeError):
                lookup.lookup(self.cfg, api=lambda *a, **kw: self.fail("no network"), now_fn=lambda: NOW)

    def test_explicit_time_window_and_missing_value(self):
        self.cfg["lookup"].update(start_time=NOW - 3600, end_time=NOW)
        self.assertEqual(lookup.time_range(self.cfg, NOW), (NOW - 3600, NOW))
        del self.cfg["lookup"]["end_time"]
        with self.assertRaises(stage0.ProbeError):
            lookup.time_range(self.cfg, NOW)

    def test_refuse_partial_pagination(self):
        self.cfg["lookup"]["max_pages"] = 1
        def api(path, **kwargs):
            if path == stage0.TENANT_TOKEN:
                return {"tenant_access_token": "tenant-secret"}
            return {"data": {"has_more": True, "page_token": "next", "meeting_briefs": []}}
        with self.assertRaisesRegex(stage0.ProbeError, "max_pages"):
            lookup.lookup(self.cfg, api=api, now_fn=lambda: NOW)

    def test_meeting_detail_failure_is_unverified(self):
        def api(path, **kwargs):
            if path == stage0.TENANT_TOKEN:
                return {"tenant_access_token": "tenant-secret"}
            if path == lookup.LIST_BY_NO:
                return {"data": {"has_more": False, "meeting_briefs": [
                    {"meeting_no": "012345678", "id": "987654321012345678"}]}}
            raise stage0.ProbeError("HTTP 403")
        self.assertEqual(lookup.lookup(self.cfg, api=api, now_fn=lambda: NOW), [
            {"meeting_id": "987654321012345678", "status": "unverified"}])


if __name__ == "__main__":
    unittest.main()
