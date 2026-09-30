import io
import json
import os
from pathlib import Path
import tempfile
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from eljev.cli import main


def _run_cli(arguments):
    output = io.StringIO()
    with redirect_stdout(output):
        code = main(arguments)
    raw = output.getvalue().strip()
    parsed = json.loads(raw) if raw else None
    return code, parsed


class InstallHookTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=False)
        self.environment.start()

    def tearDown(self):
        self.environment.stop()

    def _expected_payload(self, timeout_sec):
        python_path = Path(sys.executable).resolve().as_posix()
        hook_path = Path(__file__).resolve().parents[1].joinpath("hooks", "eljev_pre_turn.py").resolve().as_posix()
        return {
            "version": 1,
            "hooks": {
                "userPromptTransformed": [
                    {
                        "type": "command",
                        "bash": f"\"{python_path}\" \"{hook_path}\"",
                        "powershell": f"& \"{python_path}\" \"{hook_path}\"",
                        "timeoutSec": timeout_sec,
                        "comment": "el-jev pre-turn decision hook (installed by eljev install-hook)",
                    }
                ]
            },
        }

    def test_install_hook_repo_scope_writes_exact_payload(self):
        with tempfile.TemporaryDirectory() as repo:
            code, result = _run_cli(["install-hook", "--scope", "repo", "--repo", repo, "--timeout-sec", "2"])
            self.assertEqual(code, 0)
            target = Path(result["path"])
            self.assertEqual(target.resolve(), (Path(repo) / ".github" / "hooks" / "eljev.json").resolve())
            self.assertTrue(target.exists())
            payload = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(payload, self._expected_payload(2.0))

    def test_install_hook_user_scope_uses_copilot_home(self):
        with tempfile.TemporaryDirectory() as home:
            os.environ["COPILOT_HOME"] = home
            code, result = _run_cli(["install-hook", "--scope", "user"])
            self.assertEqual(code, 0)
            target = Path(result["path"])
            self.assertEqual(target.resolve(), (Path(home) / "hooks" / "eljev.json").resolve())
            payload = json.loads(target.read_text(encoding="utf-8"))
            self.assertEqual(payload, self._expected_payload(2.0))

    def test_install_hook_refuses_overwrite_without_force(self):
        with tempfile.TemporaryDirectory() as repo:
            first_code, _ = _run_cli(["install-hook", "--scope", "repo", "--repo", repo])
            self.assertEqual(first_code, 0)
            second_code, second_result = _run_cli(["install-hook", "--scope", "repo", "--repo", repo])
            self.assertEqual(second_code, 1)
            self.assertEqual(second_result["error_kind"], "invalid_input")

    def test_install_hook_force_overwrites(self):
        with tempfile.TemporaryDirectory() as repo:
            _run_cli(["install-hook", "--scope", "repo", "--repo", repo, "--timeout-sec", "1"])
            code, result = _run_cli(
                ["install-hook", "--scope", "repo", "--repo", repo, "--timeout-sec", "3", "--force"]
            )
            self.assertEqual(code, 0)
            payload = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
            self.assertEqual(payload, self._expected_payload(3.0))

    def test_uninstall_only_removes_eljev_hook(self):
        with tempfile.TemporaryDirectory() as repo:
            target = Path(repo) / ".github" / "hooks" / "eljev.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"version": 1, "hooks": {"other": []}}), encoding="utf-8")
            bad_code, bad_result = _run_cli(["uninstall-hook", "--scope", "repo", "--repo", repo])
            self.assertEqual(bad_code, 1)
            self.assertEqual(bad_result["error_kind"], "invalid_input")
            self.assertTrue(target.exists())

            install_code, _ = _run_cli(["install-hook", "--scope", "repo", "--repo", repo, "--force"])
            self.assertEqual(install_code, 0)
            uninstall_code, uninstall_result = _run_cli(["uninstall-hook", "--scope", "repo", "--repo", repo])
            self.assertEqual(uninstall_code, 0)
            self.assertEqual(uninstall_result["status"], "uninstalled")
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
