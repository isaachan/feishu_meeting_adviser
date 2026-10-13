"""Verify installation and independence from the spike's script imports."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class PackageTests(unittest.TestCase):
    def test_installed_entrypoint_runs_outside_repository(self):
        with tempfile.TemporaryDirectory() as cwd:
            result = subprocess.run([sys.executable, '-I', '-m', 'feishu_meeting_adviser', '--version'],
                                    cwd=cwd, capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), '0.1.0')

    def test_product_import_does_not_load_spike(self):
        result = subprocess.run([sys.executable, '-I', '-c',
            'import json,sys; import feishu_meeting_adviser.__main__; '
            'print(json.dumps([getattr(m,"__file__",None) for m in sys.modules.values()]))'],
            capture_output=True, text=True, check=True)
        self.assertFalse(any('reference/spike' in path for path in json.loads(result.stdout) if path))


if __name__ == '__main__':
    unittest.main()
