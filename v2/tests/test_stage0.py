import copy
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from urllib import error

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import stage0


class Stage0Tests(unittest.TestCase):
    def setUp(self):
        self.cfg = {
            "app": {"id": "app", "secret": "secret"},
            "oauth": {"redirect_uri": "https://example.org/callback", "state": "nonce", "code": "code",
                      "returned_state": "nonce", "refresh_token": "refresh", "user_access_token": "user"},
            "probe": {"timeout_seconds": 5, "max_pages": 2},
        }

    def test_default_config_is_in_v2_root(self):
        self.assertEqual(stage0.CONFIG, Path(__file__).resolve().parents[1] / "config.yaml")

    def test_auth_state_generated_and_code_cleared(self):
        cfg = copy.deepcopy(self.cfg)
        url = stage0.auth_url(cfg, save=lambda *_: None)
        self.assertIn("app_id=app", url)
        self.assertNotIn("secret", url)
        self.assertNotEqual(cfg["oauth"]["state"], "nonce")
        self.assertEqual(cfg["oauth"]["code"], "")

    def test_exchange_rejects_csrf_without_network(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["oauth"]["returned_state"] = "wrong"
        with self.assertRaises(stage0.ProbeError):
            stage0.exchange(cfg, api=lambda *a, **kw: self.fail("network should not run"))

    def test_exchange_and_refresh_update_config_not_output(self):
        calls = []
        def api(path, **kwargs):
            calls.append((path, kwargs))
            if path == stage0.APP_TOKEN:
                return {"app_access_token": "app-token"}
            return {"data": {"access_token": "new-user", "refresh_token": "new-refresh"}}
        cfg = copy.deepcopy(self.cfg)
        stage0.exchange(cfg, api=api, save=lambda *_: None)
        self.assertEqual(cfg["oauth"]["user_access_token"], "new-user")
        self.assertEqual(cfg["oauth"]["refresh_token"], "new-refresh")
        self.assertEqual(cfg["oauth"]["code"], "")
        self.assertEqual(calls[-1][1]["token"], "app-token")
        stage0.refresh(cfg, api=api, save=lambda *_: None)
        self.assertEqual(calls[-1][1]["payload"]["refresh_token"], "new-refresh")

    def test_probe_is_bounded_and_never_outputs_content_or_ids(self):
        calls = []
        def api(path, **kwargs):
            calls.append((path, kwargs))
            if path == stage0.ACTIVE:
                return {"data": {"meeting_id": "987654321012345678"}}
            return {"data": {"events": [
                {"event_type": "transcript_received", "text": "PRIVATE SENTENCE", "sentence_id": "private-id", "speaker": "PRIVATE PERSON"},
                {"event_type": "join"}], "page_token": "cursor-secret"}}
        result = stage0.probe(self.cfg, api=api)
        text = json.dumps(result)
        self.assertEqual(result["pages"], 2)
        self.assertEqual(result["event_counts"]["transcript_received"], 2)
        for secret in ("PRIVATE SENTENCE", "PRIVATE PERSON", "private-id", "987654321012345678", "cursor-secret"):
            self.assertNotIn(secret, text)
        self.assertEqual(calls[1][1]["query"], {"meeting_id": "987654321012345678"})
        self.assertEqual(calls[2][1]["query"]["page_token"], "cursor-secret")

    def test_config_permissions_and_atomic_secret_write(self):
        import yaml
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(yaml.safe_dump(self.cfg))
            os.chmod(path, 0o644)
            with self.assertRaises(stage0.ProbeError):
                stage0.load_config(path)
            os.chmod(path, 0o600)
            stage0.save_config(self.cfg, path)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stage0.load_config(path)["app"]["secret"], "secret")

    def test_http_error_hides_response(self):
        def deny(*args, **kwargs):
            raise error.HTTPError("url", 403, "PRIVATE BODY", {}, io.BytesIO(b"PRIVATE BODY"))
        with self.assertRaises(stage0.ProbeError) as raised:
            stage0.call(stage0.ACTIVE, token="secret", opener=deny)
        self.assertNotIn("PRIVATE BODY", str(raised.exception))
        self.assertNotIn("secret", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
