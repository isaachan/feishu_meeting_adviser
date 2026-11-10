import unittest

import advisor


class SkeletonTests(unittest.TestCase):
    def test_package_importable_with_version(self) -> None:
        self.assertTrue(advisor.__version__)


if __name__ == "__main__":
    unittest.main()
