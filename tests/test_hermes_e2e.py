import subprocess
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parent


class HermesRuntimeE2ETest(unittest.TestCase):
    def test_real_hermes_tool_executor_propagates_session_state(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "hermes_e2e_runner.py")],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)


if __name__ == "__main__":
    unittest.main()
