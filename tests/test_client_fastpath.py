import json
import subprocess
import sys
import time
import unittest


ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


class ClientFastPathTests(unittest.TestCase):
    def test_forbidden_modules_are_absent_when_importing_client(self):
        script = (
            "import eljev.client, sys; "
            "assert 'http.client' not in sys.modules, sorted(sys.modules); "
            "assert 'email.parser' not in sys.modules, sorted(sys.modules); "
            "assert 'argparse' not in sys.modules, sorted(sys.modules)"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)

    def test_client_import_does_not_load_forbidden_modules(self):
        script = (
            "import eljev.client, sys; "
            "print(sorted(m for m in sys.modules if m == 'http.client' "
            "or m == 'email.parser' or m == 'argparse'))"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(completed.stdout.strip(), "[]")

    def test_import_timing_can_be_measured(self):
        durations = []
        for _ in range(5):
            started = time.perf_counter()
            completed = subprocess.run(
                [sys.executable, "-c", "import eljev.client"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            durations.append((time.perf_counter() - started) * 1000.0)
            self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(len(durations), 5)


if __name__ == "__main__":
    unittest.main()
