import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

from eljev.cli import _pid_exists, main


ENV_KEYS_TO_CLEAR = (
    "EL_JEV",
    "ELJEV_ENABLED",
    "ELJEV_HOOK_MODE",
    "ELJEV_HOOK_TIMEOUT_MS",
    "ELJEV_HOOK_MIN_WORDS",
    "ELJEV_LOGGING",
    "ELJEV_ORACLE_MODE",
    "ELJEV_ORACLE_VERDICT_JSON",
    "COPILOT_HOME",
)


def _closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _run_cli(arguments, *, stdin_text=None):
    output = io.StringIO()
    stdin = io.StringIO(stdin_text) if stdin_text is not None else None
    if stdin is None:
        with redirect_stdout(output):
            code = main(arguments)
    else:
        with patch("sys.stdin", stdin), redirect_stdout(output):
            code = main(arguments)
    raw = output.getvalue()
    decoded = json.loads(raw) if raw else None
    return code, decoded


class StubHTTPServer:
    def __init__(self, response, request_callback=None):
        self.response = response
        self.request_callback = request_callback
        self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.socket.bind(("127.0.0.1", 0))
        self.socket.listen(1)
        self.port = self.socket.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.socket.close()
        self.thread.join(timeout=2)

    def _serve(self):
        try:
            connection, _ = self.socket.accept()
        except OSError:
            return
        with connection:
            connection.settimeout(2)
            data = bytearray()
            while b"\r\n\r\n" not in data:
                chunk = connection.recv(4096)
                if not chunk:
                    return
                data.extend(chunk)
            header_end = data.find(b"\r\n\r\n")
            headers = data[:header_end].decode("ascii").split("\r\n")
            content_length = 0
            for line in headers[1:]:
                if line.lower().startswith("content-length:"):
                    content_length = int(line.split(":", 1)[1].strip())
            while len(data) - header_end - 4 < content_length:
                chunk = connection.recv(4096)
                if not chunk:
                    return
                data.extend(chunk)
            if self.request_callback is not None:
                self.request_callback(bytes(data))
            body = json.dumps(self.response).encode("utf-8")
            connection.sendall(
                b"HTTP/1.1 200 OK\r\n"
                b"Content-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\n".encode("ascii")
                + b"Connection: close\r\n\r\n"
                + body
            )


class CLIExitCodeTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {}, clear=False)
        self.environment.start()
        self.tempdir = tempfile.TemporaryDirectory()
        for key in ENV_KEYS_TO_CLEAR:
            os.environ.pop(key, None)
        os.environ["ELJEV_DIR"] = self.tempdir.name
        os.environ["ELJEV_PORT"] = str(_closed_port())
        self.user_env = patch("eljev.config.user_env_value", return_value=None)
        self.user_env.start()

    def tearDown(self):
        self.user_env.stop()
        self.tempdir.cleanup()
        self.environment.stop()

    def test_pid_exists_for_current_and_dead_process(self):
        self.assertTrue(_pid_exists(os.getpid()))

        process = subprocess.Popen([sys.executable, "-c", "pass"])
        pid = process.pid
        process.wait(timeout=5)
        self.assertFalse(_pid_exists(pid))

    def test_each_cap_violation_and_malformed_input_returns_one(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = [
                ("count", {"criterion": "x", "candidates": ["x"] * 251}, "candidates.json"),
                ("per_candidate", {"criterion": "x", "candidates": ["x" * 2001]}, "candidates.json"),
                ("total", {"criterion": "x", "candidates": ["x" * 2000] * 50 + ["x"]}, "candidates.json"),
                (
                    "duplicate",
                    {"criterion": "x", "candidates": [{"id": "a", "text": "one"}, {"id": "a", "text": "two"}]},
                    "candidates.json",
                ),
                ("empty", {"criterion": "x", "candidates": [" "]}, "candidates.json"),
                ("criterion", {"criterion": " ", "candidates": ["x"]}, "candidates.json"),
            ]
            for case_name, payload, filename in cases:
                path = root / filename
                path.write_text(json.dumps(payload["candidates"]), encoding="utf-8")
                code, result = _run_cli(
                    ["screen", "--criterion", payload["criterion"], "--candidates", str(path)]
                )
                self.assertEqual(code, 1, case_name)
                self.assertEqual(result["error_kind"], "invalid_input", case_name)

            malformed = root / "malformed.json"
            malformed.write_text("{", encoding="utf-8")
            code, result = _run_cli(
                ["screen", "--criterion", "x", "--candidates", str(malformed)]
            )
            self.assertEqual(code, 1)
            self.assertEqual(result["error_kind"], "invalid_input")

    def test_daemon_down_is_exit_two_with_start_note(self):
        os.environ["ELJEV_PORT"] = str(_closed_port())
        code, result = _run_cli(
            ["screen", "--criterion", "choose", "--text", "one candidate"]
        )
        self.assertEqual(code, 2)
        self.assertEqual(result["error_kind"], "daemon_unavailable")
        self.assertIn("eljev daemon start", result["notes"][0])

    def test_decision_record_is_passed_through(self):
        record = {
            "schema": "eljev.decision/1",
            "decision_id": "fixed",
            "ts": "2026-09-24T18:00:00.000Z",
            "shape": "screen",
            "criterion": "choose",
            "n_candidates": 2,
            "tier_path": ["pregate", "cohere"],
            "engine": "cohere-rerank-v4.0-pro",
            "engine_version": "1",
            "choice": None,
            "choice_index": None,
            "status": "needs_review",
            "exit_code": 2,
            "raw_top_score": 0.8,
            "raw_runner_up": 0.7,
            "margin_raw": 0.1,
            "calibrated_probability": None,
            "margin_calibrated": None,
            "calibration_version": "none",
            "coverage_policy": "always_abstain_v0",
            "results": [{"id": "c0", "index": 0, "relevance_score": 0.8}],
            "elapsed_ms": {"total": 1.0, "pregate": 0.1, "engine": 0.8, "overhead": 0.1},
            "escalation_reason": None,
            "error_kind": None,
            "notes": [],
        }
        with StubHTTPServer(record) as server:
            os.environ["ELJEV_PORT"] = str(server.port)
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".json", encoding="utf-8", delete=False
            ) as stream:
                json.dump([{"id": "c0", "text": "one"}, {"id": "c1", "text": "two"}], stream)
                path = stream.name
            try:
                code, result = _run_cli(
                    ["screen", "--criterion", "choose", "--candidates", path]
                )
            finally:
                os.unlink(path)
        self.assertEqual(code, 2)
        self.assertEqual(result, record)

    def test_stale_pidfile_uses_eljev_dir_and_reports_cleanly(self):
        os.environ["ELJEV_PORT"] = str(_closed_port())
        pidfile = Path(os.environ["ELJEV_DIR"]) / "daemon.pid"
        pidfile.parent.mkdir(parents=True, exist_ok=True)
        pidfile.write_text("429496729\n", encoding="ascii")
        code, result = _run_cli(["daemon", "status"])
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "stopped")
        self.assertTrue(result["stale_pidfile"])

    def test_configure_validation_and_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            os.environ["ELJEV_DIR"] = directory

            bad_url_code, bad_url_result = _run_cli(["configure", "--endpoint", "http://example.com"])
            self.assertEqual(bad_url_code, 1)
            self.assertEqual(bad_url_result["error_kind"], "invalid_input")

            bad_policy_code, bad_policy_result = _run_cli(["configure", "--policy", "sometimes"])
            self.assertEqual(bad_policy_code, 1)
            self.assertEqual(bad_policy_result["error_kind"], "invalid_input")

            code, result = _run_cli(
                [
                    "configure",
                    "--endpoint",
                    "https://example.contoso.com/",
                    "--deployment",
                    "my-deployment",
                    "--policy",
                    "calibrated",
                    "--auth",
                    "managed_identity",
                    "--hook-mode",
                    "marker",
                ]
            )
            self.assertEqual(code, 0)
            self.assertEqual(result["status"], "configured")
            self.assertEqual(result["config"]["cohere_endpoint"], "https://example.contoso.com")
            self.assertEqual(result["config"]["cohere_deployment"], "my-deployment")
            self.assertEqual(result["config"]["coverage_policy"], "calibrated")
            self.assertEqual(result["config"]["auth"], "managed_identity")
            self.assertEqual(result["config"]["hook_mode"], "marker")

            status_code, status_result = _run_cli(["status"])
            self.assertEqual(status_code, 0)
            self.assertIn("enabled", status_result)
            self.assertIn("source", status_result)
            self.assertIn("hook_mode", status_result)
            self.assertIn("daemon_status", status_result)
            self.assertIn("daemon_pid", status_result)
            self.assertIn("cohere_endpoint_configured", status_result)
            self.assertIn("cohere_deployment", status_result)
            self.assertIn("auth_mode", status_result)
            self.assertIn("coverage_policy", status_result)
            self.assertIn("logging_enabled", status_result)
            self.assertIn("config_path", status_result)
            self.assertIn("log_dir", status_result)

    def test_on_off_include_override_note_when_env_forces_value(self):
        os.environ["EL_JEV"] = "OFF"
        with patch("eljev.cli._daemon_start_result", return_value=(0, {"status": "ok", "pid": 1})):
            code, result = _run_cli(["on"])
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "enabled")
        self.assertFalse(result["effective"])
        self.assertEqual(result["source"], "env:EL_JEV")
        self.assertIn("notes", result)

        with patch("eljev.cli._daemon_stop_result", return_value=(0, {"status": "stopped", "pid": 1})):
            code, result = _run_cli(["off"])
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "disabled")
        self.assertFalse(result["effective"])
        self.assertEqual(result["source"], "env:EL_JEV")

    def test_daemon_stop_handles_orphan_without_pidfile(self):
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        dead_pid = process.pid
        process.wait(timeout=5)
        fake_client = MagicMock()
        fake_client.health.return_value = {"status": "ok", "pid": dead_pid}
        fake_client.shutdown.return_value = {"status": "stopping", "pid": dead_pid}

        with patch("eljev.cli.EljevClient", return_value=fake_client):
            code, result = _run_cli(["daemon", "stop"])

        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "stopped")
        self.assertEqual(result["pid"], dead_pid)
        fake_client.shutdown.assert_called_once()

    def test_daemon_start_when_healthy_does_not_spawn(self):
        fake_client = MagicMock()
        fake_client.health.return_value = {"status": "ok", "pid": 123}
        with patch("eljev.cli.EljevClient", return_value=fake_client):
            with patch("eljev.cli._spawn_daemon") as spawn_daemon:
                code, result = _run_cli(["daemon", "start"])
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "ok")
        spawn_daemon.assert_not_called()

    def test_daemon_start_when_child_exits_early_reports_starting(self):
        process = subprocess.Popen([sys.executable, "-c", "pass"])
        dead_pid = process.pid
        process.wait(timeout=5)
        fake_client = MagicMock()
        fake_client.health.return_value = {"status": "down"}
        with patch("eljev.cli.EljevClient", return_value=fake_client):
            with patch("eljev.cli._spawn_daemon", return_value=dead_pid):
                with patch("eljev.cli.START_TIMEOUT_S", 0.05):
                    code, result = _run_cli(["daemon", "start"])
        self.assertEqual(code, 2)
        self.assertEqual(result["error_kind"], "daemon_starting")
        self.assertEqual(result["status"], "starting")

    def test_log_rejects_invalid_limit(self):
        code, result = _run_cli(["log", "--limit", "0"])
        self.assertEqual(code, 1)
        self.assertEqual(result["error_kind"], "invalid_input")

    def test_configure_rejects_invalid_hook_mode(self):
        code, result = _run_cli(["configure", "--hook-mode", "bogus"])
        self.assertEqual(code, 1)
        self.assertEqual(result["error_kind"], "invalid_input")

    def test_version_reports_semver(self):
        code, result = _run_cli(["version"])
        self.assertEqual(code, 0)
        self.assertEqual(result, {"version": "0.2.0"})


if __name__ == "__main__":
    unittest.main()
