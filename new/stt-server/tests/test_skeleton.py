import unittest

import stt_server


class SkeletonTests(unittest.TestCase):
    def test_package_importable_with_version(self) -> None:
        self.assertTrue(stt_server.__version__)


if __name__ == "__main__":
    unittest.main()
