import unittest

import advisor
import auth
import bot
import llm_gateway
import recorder
import stt_server


class SkeletonTests(unittest.TestCase):
    def test_all_six_modules_importable(self) -> None:
        for module in (advisor, auth, bot, llm_gateway, recorder, stt_server):
            with self.subTest(module=module.__name__):
                self.assertTrue(module.__version__)


if __name__ == "__main__":
    unittest.main()
