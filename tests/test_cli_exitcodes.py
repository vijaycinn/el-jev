import io
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from eljev.cli import main


ROOT = Path(__file__).resolve().parents[1]


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

    def tearDown(self):
        self.environment.stop()

    def test_each_cap_violation_and_malformed_input_returns_one(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cases = [
                (
                    "count",
                    {"criterion": "x", "candidates": ["x"] * 251},
                    "candidates.json",
                ),
                (
                    "per_candidate",
                    {"criterion": "x", "candidates": ["x" * 2001]},
                    "candidates.json",
                ),
                (
                    "total",
                    {"criterion": "x", "candidates": ["x" * 2000] * 50 + ["x"]},
                    "candidates.json",
                ),
                (
                    "duplicate",
                    {
                        "criterion": "x",
                        "candidates": [{"id": "a", "text": "one"}, {"id": "a", "text": "two"}],
                    },
                    "candidates.json",
                ),
                (
                    "empty",
                    {"criterion": "x", "candidates": [" "]},
                    "candidates.json",
                ),
                (
                    "criterion",
                    {"criterion": " ", "candidates": ["x"]},
                    "candidates.json",
                ),
            ]
            for _name, payload, filename in cases:
                path = root / filename
                path.write_text(json.dumps(payload["candidates"]), encoding="utf-8")
                code, result = _run_cli(
                    [
                        "screen",
                        "--criterion",
                        payload["criterion"],
                        "--candidates",
                        str(path),
                    ]
                )
                self.assertEqual(code, 1, _name)
                self.assertEqual(result["error_kind"], "invalid_input", _name)

            malformed = root / "malformed.json"
            malformed.write_text("{", encoding="utf-8")
            code, result = _run_cli(
                ["screen", "--criterion", "x", "--candidates", str(malformed)]
            )
            self.assertEqual(code, 1)
            self.assertEqual(result["error_kind"], "invalid_input")

    def test_daemon_down_is_exit_two_with_start_note(self):
        os.environ["ELJEV_PORT"] = "1"
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

    def test_stdin_candidates_and_text_convenience(self):
        received = []
        record = {"schema": "eljev.decision/1", "status": "trivial", "exit_code": 2}

        def capture(request):
            received.append(request.decode("utf-8"))

        with StubHTTPServer(record, capture) as server:
            os.environ["ELJEV_PORT"] = str(server.port)
            code, result = _run_cli(
                ["screen", "--criterion", "choose", "--candidates", "-"],
                stdin_text='["first", "second"]',
            )
        self.assertEqual(code, 2)
        self.assertEqual(result, record)
        self.assertIn('"candidates":[{"id":"c0","text":"first"},{"id":"c1","text":"second"}]', received[0])

        received.clear()
        with StubHTTPServer(record, capture) as server:
            os.environ["ELJEV_PORT"] = str(server.port)
            code, result = _run_cli(
                ["screen", "--criterion", "choose", "--text", "one candidate"]
            )
        self.assertEqual(code, 2)
        self.assertEqual(result, record)
        self.assertIn('"candidates":[{"id":"c0","text":"one candidate"}]', received[0])

    def test_stale_pidfile_is_reported_cleanly(self):
        with tempfile.TemporaryDirectory() as home:
            os.environ["USERPROFILE"] = home
            os.environ["HOME"] = home
            pidfile = Path(home) / ".eljev" / "daemon.pid"
            pidfile.parent.mkdir(parents=True)
            pidfile.write_text("429496729\n", encoding="ascii")
            code, result = _run_cli(["daemon", "status"])
        self.assertEqual(code, 0)
        self.assertEqual(result["status"], "stopped")
        self.assertTrue(result["stale_pidfile"])


if __name__ == "__main__":
    unittest.main()
