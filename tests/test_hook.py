import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
HOOK_PATH = ROOT / "hooks" / "eljev_pre_turn.py"
ENV_KEYS_TO_CLEAR = (
    "EL_JEV",
    "ELJEV_ENABLED",
    "ELJEV_HOOK_MODE",
    "ELJEV_HOOK_TIMEOUT_MS",
    "ELJEV_HOOK_MIN_WORDS",
    "ELJEV_LOGGING",
    "ELJEV_ORACLE_MODE",
    "ELJEV_ORACLE_VERDICT_JSON",
    "ELJEV_HOST",
    "COPILOT_HOME",
)


def _closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _load_hook_module():
    spec = importlib.util.spec_from_file_location("eljev_pre_turn", HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _request_body(raw_request: bytes) -> dict[str, object]:
    header, _, body = raw_request.partition(b"\r\n\r\n")
    headers = header.decode("ascii", "replace").split("\r\n")
    content_length = 0
    for line in headers[1:]:
        if line.lower().startswith("content-length:"):
            content_length = int(line.split(":", 1)[1].strip())
            break
    payload = body[:content_length] if content_length else body
    return json.loads(payload.decode("utf-8"))


class StubHTTPServer:
    def __init__(self, response, *, status_code=200, request_callback=None):
        self.response = response
        self.status_code = status_code
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
            headers = data[:header_end].decode("ascii", "replace").split("\r\n")
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
            response_body = json.dumps(self.response).encode("utf-8")
            connection.sendall(
                f"HTTP/1.1 {self.status_code} STATUS\r\n".encode("ascii")
                + b"Content-Type: application/json\r\n"
                + f"Content-Length: {len(response_body)}\r\n".encode("ascii")
                + b"Connection: close\r\n\r\n"
                + response_body
            )


class HookTests(unittest.TestCase):
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
        self.hook = _load_hook_module()

    def tearDown(self):
        self.user_env.stop()
        self.tempdir.cleanup()
        self.environment.stop()

    def _run_hook(self, payload_text: str) -> tuple[int, dict[str, object]]:
        stdout = io.StringIO()
        with patch("sys.stdin", io.StringIO(payload_text)), patch("sys.stdout", stdout):
            code = self.hook.main()
        lines = [line for line in stdout.getvalue().splitlines() if line.strip()]
        self.assertEqual(len(lines), 1)
        return code, json.loads(lines[0])

    def _run_hook_json(self, payload: dict[str, object]) -> tuple[int, dict[str, object]]:
        return self._run_hook(json.dumps(payload))

    def _decision(self, *, status="selected", exit_code=0, choice="go", probabilities=None):
        return {
            "schema": "eljev.decision/1",
            "decision_id": "intent-id-123",
            "shape": "decide",
            "kind": "choice",
            "choice": choice,
            "status": status,
            "exit_code": exit_code,
            "probabilities": probabilities or {},
        }

    def test_disabled_el_jev_returns_empty_without_socket(self):
        os.environ["EL_JEV"] = "OFF"
        with patch.object(
            self.hook.socket,
            "create_connection",
            side_effect=AssertionError("network should not be called"),
        ):
            code, result = self._run_hook_json({"transformedPrompt": "Should I deploy this?"})
        self.assertEqual(code, 0)
        self.assertEqual(result, {})

    def test_invalid_json_stdin_returns_empty(self):
        code, result = self._run_hook("{")
        self.assertEqual(code, 0)
        self.assertEqual(result, {})

    def test_marker_mode_plain_prompt_skips_network(self):
        os.environ["ELJEV_HOOK_MODE"] = "marker"
        with patch.object(
            self.hook.socket,
            "create_connection",
            side_effect=AssertionError("network should not be called"),
        ):
            code, result = self._run_hook_json({"transformedPrompt": "Please review this patch"})
        self.assertEqual(code, 0)
        self.assertEqual(result, {})

    def test_marker_mode_marker_prompt_injects_advisory_and_removes_marker(self):
        os.environ["ELJEV_HOOK_MODE"] = "marker"
        marker_request = {
            "path": "/v1/decide",
            "state": "state",
            "question": {"id": "q", "kind": "choice", "criteria": {"go": "go", "stop": "stop"}},
        }
        prompt = (
            f"Prefix {self.hook.MARKER_START}{json.dumps(marker_request)}"
            f"{self.hook.MARKER_END} suffix"
        )
        with StubHTTPServer(self._decision(status="needs_review", exit_code=2)) as server:
            os.environ["ELJEV_PORT"] = str(server.port)
            code, result = self._run_hook_json({"transformedPrompt": prompt, "sessionId": "s-1"})
        self.assertEqual(code, 0)
        transformed = result.get("modifiedTransformedPrompt", "")
        self.assertNotIn(self.hook.MARKER_START, transformed)
        self.assertIn("[el-jev advisory]", transformed)
        self.assertIn("Prefix", transformed)
        self.assertIn("suffix", transformed)

    def test_intent_mode_classifies_plain_prompt_with_choice_criteria(self):
        captured: list[bytes] = []
        probabilities = {
            "code_modification": 0.92,
            "review_audit": 0.55,
            "execution_testing": 0.45,
            "advisory_explanation": 0.12,
        }
        response = self._decision(
            status="needs_review",
            exit_code=2,
            choice="intent-id-123",
            probabilities=probabilities,
        )
        with StubHTTPServer(response, request_callback=lambda raw: captured.append(raw)) as server:
            os.environ["ELJEV_PORT"] = str(server.port)
            code, result = self._run_hook_json(
                {"transformedPrompt": "Should I deploy this migration to production?"}
            )
        self.assertEqual(code, 0)
        self.assertTrue(captured)
        request = _request_body(captured[0])
        self.assertEqual(request["question"]["kind"], "choice")
        self.assertEqual(
            list(request["question"]["criteria"].keys()),
            list(self.hook.INTENT_CRITERIA.keys()),
        )
        transformed = result["modifiedTransformedPrompt"]
        self.assertIn("gate: needs_review (exit 2)", transformed)
        self.assertIn("intent-id-123", transformed)
        probability_lines = [line for line in transformed.splitlines() if line.startswith("probabilities:")]
        self.assertEqual(len(probability_lines), 1)
        rendered = [piece.strip() for piece in probability_lines[0].split(":", 1)[1].split(",") if piece.strip()]
        self.assertLessEqual(len(rendered), 3)

    def test_selected_exit_zero_includes_selected_gate_line(self):
        with StubHTTPServer(self._decision(status="selected", exit_code=0)) as server:
            os.environ["ELJEV_PORT"] = str(server.port)
            code, result = self._run_hook_json({"transformedPrompt": "Should I ship this change now?"})
        self.assertEqual(code, 0)
        self.assertIn("gate: selected (exit 0)", result["modifiedTransformedPrompt"])

    def test_min_words_and_max_prompt_length_controls(self):
        prompt = {"transformedPrompt": "fix it"}
        with patch.object(
            self.hook.socket,
            "create_connection",
            side_effect=AssertionError("network should not be called"),
        ):
            code, result = self._run_hook_json(prompt)
        self.assertEqual(code, 0)
        self.assertEqual(result, {})

        os.environ["ELJEV_HOOK_MIN_WORDS"] = "2"
        with StubHTTPServer(self._decision(status="selected", exit_code=0)) as server:
            os.environ["ELJEV_PORT"] = str(server.port)
            _, min_words_result = self._run_hook_json(prompt)
        self.assertIn("modifiedTransformedPrompt", min_words_result)

        long_prompt = {"transformedPrompt": "x" * 4001}
        with patch.object(
            self.hook.socket,
            "create_connection",
            side_effect=AssertionError("network should not be called"),
        ):
            _, long_result = self._run_hook_json(long_prompt)
        self.assertEqual(long_result, {})

    def test_fail_open_for_engine_error_wrong_schema_and_http_error(self):
        cases = [
            ("engine_error", self._decision(status="engine_error", exit_code=2), 200),
            ("wrong_schema", {"schema": "not.eljev.decision/1"}, 200),
            ("http_500", self._decision(status="selected", exit_code=0), 500),
        ]
        for name, response, status_code in cases:
            with self.subTest(case=name):
                with StubHTTPServer(response, status_code=status_code) as server:
                    os.environ["ELJEV_PORT"] = str(server.port)
                    code, result = self._run_hook_json(
                        {"transformedPrompt": "Should I deploy this migration to production?"}
                    )
                self.assertEqual(code, 0)
                self.assertEqual(result, {})

    def test_request_headers_include_content_type_and_host_without_origin(self):
        captured: list[bytes] = []
        with StubHTTPServer(
            self._decision(status="selected", exit_code=0),
            request_callback=lambda raw: captured.append(raw),
        ) as server:
            os.environ["ELJEV_PORT"] = str(server.port)
            self._run_hook_json({"transformedPrompt": "Should I run these tests before merging?"})
        self.assertTrue(captured)
        raw_text = captured[0].decode("ascii", "replace").lower().replace("\r", "")
        self.assertIn("post /v1/decide http/1.1", raw_text)
        self.assertIn(f"host: 127.0.0.1:{server.port}", raw_text)
        self.assertIn("content-type: application/json", raw_text)
        self.assertNotIn("\norigin:", raw_text)

    def test_daemon_down_triggers_single_spawn_with_debounce(self):
        os.environ["ELJEV_PORT"] = str(_closed_port())
        payload = {"transformedPrompt": "Should I deploy this migration to production?"}
        with patch.object(self.hook.subprocess, "Popen", autospec=True) as popen:
            popen.return_value = types.SimpleNamespace(pid=4242)
            start = time.perf_counter()
            code_first, result_first = self._run_hook_json(payload)
            first_elapsed = time.perf_counter() - start
            start = time.perf_counter()
            code_second, result_second = self._run_hook_json(payload)
            second_elapsed = time.perf_counter() - start

        self.assertEqual(code_first, 0)
        self.assertEqual(result_first, {})
        self.assertEqual(code_second, 0)
        self.assertEqual(result_second, {})
        self.assertLess(first_elapsed, 1.0)
        self.assertLess(second_elapsed, 1.0)
        popen.assert_called_once()
        args, kwargs = popen.call_args
        self.assertEqual(args[0], [sys.executable, "-m", "eljev", "_daemon-run"])
        self.assertNotIn("endpoint", " ".join(args[0]).lower())
        self.assertTrue((Path(os.environ["ELJEV_DIR"]) / "spawn.stamp").exists())
        self.assertIn("env", kwargs)

    def test_generic_payload_returns_hook_result_schema(self):
        payload = {"schema": "eljev.hook/1", "criterion": "pick", "candidates": ["a", "b"]}
        with StubHTTPServer(self._decision(status="selected", exit_code=0)) as server:
            os.environ["ELJEV_PORT"] = str(server.port)
            code, result = self._run_hook_json(payload)
        self.assertEqual(code, 0)
        self.assertEqual(result["schema"], "eljev.hook_result/1")
        self.assertEqual(result["decision"]["schema"], "eljev.decision/1")

    def test_oracle_mode_uses_oracle_without_network(self):
        os.environ["ELJEV_ORACLE_MODE"] = "1"
        os.environ["ELJEV_ORACLE_VERDICT_JSON"] = json.dumps(
            self._decision(status="selected", exit_code=0, choice="oracle-choice")
        )
        with patch.object(
            self.hook.socket,
            "create_connection",
            side_effect=AssertionError("network should not be called"),
        ):
            code, result = self._run_hook_json(
                {"transformedPrompt": "Should I deploy this migration to production?"}
            )
        self.assertEqual(code, 0)
        self.assertIn("oracle-choice", result["modifiedTransformedPrompt"])

    def test_hook_logging_obeys_switch_and_omits_prompt_text(self):
        prompt_text = "TOP_SECRET_PROMPT_TEXT_12345 should never appear in logs"
        oracle = self._decision(status="selected", exit_code=0)

        os.environ["ELJEV_ORACLE_MODE"] = "1"
        os.environ["ELJEV_ORACLE_VERDICT_JSON"] = json.dumps(oracle)
        os.environ["ELJEV_LOGGING"] = "OFF"
        _, off_result = self._run_hook_json({"transformedPrompt": prompt_text})
        self.assertIn("modifiedTransformedPrompt", off_result)
        log_path = Path(os.environ["ELJEV_DIR"]) / "logs" / "hook.jsonl"
        self.assertFalse(log_path.exists())

        os.environ.pop("ELJEV_LOGGING", None)
        _, on_result = self._run_hook_json({"transformedPrompt": prompt_text})
        self.assertIn("modifiedTransformedPrompt", on_result)
        self.assertTrue(log_path.exists())
        lines = log_path.read_text(encoding="utf-8")
        self.assertNotIn(prompt_text, lines)


if __name__ == "__main__":
    unittest.main()
