import unittest

import recorder


class SkeletonTests(unittest.TestCase):
    def test_package_importable_with_version(self) -> None:
        self.assertTrue(recorder.__version__)


if __name__ == "__main__":
    unittest.main()
