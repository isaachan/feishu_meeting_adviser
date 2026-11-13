import unittest

import bot


class SkeletonTests(unittest.TestCase):
    def test_package_importable_with_version(self) -> None:
        self.assertTrue(bot.__version__)


if __name__ == "__main__":
    unittest.main()
