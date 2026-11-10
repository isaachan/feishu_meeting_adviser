import unittest

import llm_gateway


class SkeletonTests(unittest.TestCase):
    def test_package_importable_with_version(self) -> None:
        self.assertTrue(llm_gateway.__version__)


if __name__ == "__main__":
    unittest.main()
