import json
from pathlib import Path
from types import SimpleNamespace
import os
import tempfile
import unittest

from chatbot.backend import OpenCodeBackend
from chatbot.bot import ChatBot
from chatbot.config import Settings, load_settings
from chatbot.context import read_context


def settings(executable="opencode"):
    return Settings("cli_test", "secret", executable, "provider/model", "test-key",
                    5, 1000, 2, 100, 10)


class ConfigAndContextTests(unittest.TestCase):
    def test_config_requires_credentials_in_yaml(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "config.yaml"
            path.write_text("feishu: {}\nopencode: {}\nchat: {}\n", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "model"):
                load_settings(path)

    def test_context_reads_text_and_skips_hidden_or_symlinks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "guide.md").write_text("background", encoding="utf-8")
            (root / ".secret.md").write_text("hidden", encoding="utf-8")
            (root / "linked.md").symlink_to(root / "guide.md")
            self.assertIn("background", read_context(root, 100))
            self.assertNotIn("hidden", read_context(root, 100))
            with self.assertRaisesRegex(ValueError, "超过"):
                read_context(root, 3)


class BackendTests(unittest.TestCase):
    def test_opencode_json_output_and_no_inherited_configuration(self):
        with tempfile.TemporaryDirectory() as temp:
            executable = Path(temp) / "fake-opencode"
            executable.write_text(
                "#!/bin/sh\n"
                "test -z \"${BAD_ENV+x}\" || exit 7\n"
                "test -n \"$OPENCODE_AUTH_CONTENT\" || exit 8\n"
                "printf '%s\\n' '{\"type\":\"text\",\"part\":{\"text\":\"你好\"}}'\n",
                encoding="utf-8")
            executable.chmod(0o700)
            old = os.environ.get("BAD_ENV")
            os.environ["BAD_ENV"] = "should-not-pass"
            try:
                self.assertEqual(OpenCodeBackend(settings(str(executable))).reply("hi"), "你好")
            finally:
                if old is None:
                    os.environ.pop("BAD_ENV", None)
                else:
                    os.environ["BAD_ENV"] = old


class MessageTests(unittest.TestCase):
    def test_group_mention_and_duplicate_filter(self):
        with tempfile.TemporaryDirectory() as temp:
            bot = ChatBot(settings(), Path(temp), object())
            message = SimpleNamespace(message_id="om_1", chat_id="oc_1",
                                      chat_type="group", message_type="text",
                                      content=json.dumps({"text": "@机器人 你好"}),
                                      mentions=[])
            event = SimpleNamespace(event=SimpleNamespace(message=message,
                                    sender=SimpleNamespace(sender_type="user")))
            bot.on_message(event)
            self.assertTrue(bot.queue.empty())
            message.mentions = [SimpleNamespace(key="@机器人")]
            bot.on_message(event)
            bot.on_message(event)
            self.assertEqual(bot.queue.qsize(), 1)
            self.assertEqual(bot.queue.get_nowait(), ("om_1", "oc_1", "你好"))


if __name__ == "__main__":
    unittest.main()
