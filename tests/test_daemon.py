import http.client
import json
import os
from pathlib import Path
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from eljev import daemon
from eljev.engines.systemone import SystemOneError


class _FakeCohere:
    def __init__(self, endpoint: str = "") -> None:
        self.endpoint = endpoint
        self.closed = False

    def warm(self) -> bool:
        return False

    def close(self) -> None:
        self.closed = True


class _FakeSystemOne:
    def __init__(self, behavior) -> None:
        self.behavior = behavior
        self.calls: list[tuple[object, object]] = []

    def decide(self, state, question):  # noqa: ANN001
        self.calls.append((state, question))
        if isinstance(self.behavior, BaseException):
            raise self.behavior
        if callable(self.behavior):
            return self.behavior(state, question)
        return self.behavior


class _FakeMD1:
    def __init__(self, *, decide_behavior=None, screen_behavior=None, gate_behavior=None) -> None:
        self.decide_behavior = decide_behavior
        self.screen_behavior = screen_behavior
        self.gate_behavior = gate_behavior
        self.decide_calls: list[tuple[object, object]] = []
        self.screen_calls: list[tuple[str, list[object], int | None]] = []
        self.gate_calls: list[tuple[object, str | None]] = []

    def decide(self, state, question):  # noqa: ANN001
        self.decide_calls.append((state, question))
        if isinstance(self.decide_behavior, BaseException):
            raise self.decide_behavior
        if callable(self.decide_behavior):
            return self.decide_behavior(state, question)
        return self.decide_behavior

    def screen(self, criterion: str, candidates, *, top_n=None):  # noqa: ANN001
        self.screen_calls.append((criterion, list(candidates), top_n))
        if isinstance(self.screen_behavior, BaseException):
            raise self.screen_behavior
        if callable(self.screen_behavior):
            return self.screen_behavior(criterion, candidates, top_n=top_n)
        return self.screen_behavior

    def gate(self, state, *, session_id=None):  # noqa: ANN001
        self.gate_calls.append((state, session_id))
        if isinstance(self.gate_behavior, BaseException):
            raise self.gate_behavior
        if callable(self.gate_behavior):
            return self.gate_behavior(state, session_id=session_id)
        return self.gate_behavior


class _FakeRunCohere:
    def __init__(self, *args, **kwargs) -> None:  # noqa: ANN002, ANN003
        self.endpoint = ""

    def warm(self) -> bool:
        return False

    def close(self) -> None:
        return


class DaemonTests(unittest.TestCase):
    ENV_KEYS = (
        "ELJEV_DIR",
        "ELJEV_COVERAGE_POLICY",
        "ELJEV_CALIBRATION_PATH",
        "ELJEV_COHERE_ENDPOINT",
        "ELJEV_SYSTEMONE_URL",
        "ELJEV_AUTH",
        "EL_JEV",
        "ELJEV_PORT",
        "ELJEV_LOGGING",
        "ELJEV_LOG_DIR",
    )

    def setUp(self) -> None:
        self._environment = patch.dict(os.environ, {}, clear=False)
        self._environment.start()
        self._tempdir = tempfile.TemporaryDirectory()
        self._clear_relevant_env()
        os.environ["ELJEV_DIR"] = self._tempdir.name
        self._servers: list[tuple[daemon.EljevHTTPServer, threading.Thread]] = []

    def tearDown(self) -> None:
        for server, thread in self._servers:
            try:
                server.shutdown()
            except Exception:
                pass
            try:
                server.server_close()
            except Exception:
                pass
            thread.join(timeout=2.0)
        self._environment.stop()
        self._tempdir.cleanup()

    def _clear_relevant_env(self) -> None:
        for key in self.ENV_KEYS:
            os.environ.pop(key, None)

    def _start_server(
        self,
        *,
        systemone_behavior=None,
        cohere_endpoint: str = "",
        md1_endpoint: str = "",
        md1_decide_behavior=None,
        md1_screen_behavior=None,
        md1_gate_behavior=None,
    ) -> tuple[daemon.EljevHTTPServer, threading.Thread]:
        default_result = {
            "schema": "eljev.decision/1",
            "shape": "decide",
            "status": "needs_review",
            "exit_code": 2,
            "choice": "alpha",
        }
        behavior = default_result if systemone_behavior is None else systemone_behavior
        server = daemon.create_server(port=0)
        server.cohere = _FakeCohere(endpoint=cohere_endpoint)
        server.systemone = _FakeSystemOne(behavior)
        server.md1_client = type("MD1ClientStub", (), {"endpoint": md1_endpoint})()
        server.md1 = _FakeMD1(
            decide_behavior=md1_decide_behavior,
            screen_behavior=md1_screen_behavior,
            gate_behavior=md1_gate_behavior,
        )
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self._servers.append((server, thread))
        return server, thread

    def _request(
        self,
        server: daemon.EljevHTTPServer,
        method: str,
        path: str,
        *,
        body: object | None = None,
        headers: dict[str, str] | None = None,
        host: str | None = None,
    ) -> tuple[int, object]:
        data = b""
        if body is None:
            data = b""
        elif isinstance(body, bytes):
            data = body
        elif isinstance(body, str):
            data = body.encode("utf-8")
        else:
            data = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")

        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=2.0)
        try:
            conn.putrequest(method, path, skip_host=True)
            conn.putheader("Host", host or f"127.0.0.1:{server.server_port}")
            for key, value in (headers or {}).items():
                conn.putheader(key, value)
            conn.putheader("Content-Length", str(len(data)))
            conn.endheaders(data)
            response = conn.getresponse()
            raw_body = response.read()
        finally:
            conn.close()

        text = raw_body.decode("utf-8", errors="replace") if raw_body else ""
        try:
            parsed = json.loads(text) if text else {}
        except json.JSONDecodeError:
            parsed = text
        return response.status, parsed

    def _valid_decide_payload(self) -> dict[str, object]:
        return {
            "state": "billing issue with duplicate charge",
            "question": {
                "id": "q1",
                "kind": "choice",
                "instructions": "route",
                "options": [
                    {"id": "alpha", "description": "billing and refunds"},
                    {"id": "beta", "description": "security and auth"},
                ],
            },
        }

    def test_health_contains_pid_and_version(self):
        server, _ = self._start_server()
        status, body = self._request(server, "GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["pid"], os.getpid())
        self.assertEqual(body["version"], "0.2.0")
        self.assertEqual(body["engines"]["md1"], "absent")

    def test_health_reports_md1_ready_when_md1_endpoint_is_configured(self):
        server, _ = self._start_server(md1_endpoint="https://vcinn.services.ai.azure.com")
        status, body = self._request(server, "GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(body["engines"]["md1"], "ready")

    def test_origin_header_is_forbidden(self):
        server, _ = self._start_server()
        status, body = self._request(
            server,
            "GET",
            "/health",
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(body["error_kind"], "forbidden")

    def test_non_local_host_header_is_forbidden(self):
        server, _ = self._start_server()
        status, body = self._request(server, "GET", "/health", host="evil.example")
        self.assertEqual(status, 403)
        self.assertEqual(body["error_kind"], "forbidden")

    def test_decide_requires_application_json_content_type(self):
        server, _ = self._start_server()
        status, body = self._request(
            server,
            "POST",
            "/v1/decide",
            body="{}",
            headers={"Content-Type": "text/plain"},
        )
        self.assertEqual(status, 415)
        self.assertEqual(body["error_kind"], "invalid_input")

    def test_invalid_decide_request_body_returns_400_with_exit_code_1(self):
        server, _ = self._start_server()
        status, body = self._request(
            server,
            "POST",
            "/v1/decide",
            body={},
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["exit_code"], 1)
        self.assertEqual(body["error_kind"], "invalid_input")

    def test_systemone_invalid_input_returns_400(self):
        server, _ = self._start_server(
            systemone_behavior=SystemOneError("invalid_input", "invalid question")
        )
        status, body = self._request(
            server,
            "POST",
            "/v1/decide",
            body=self._valid_decide_payload(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["exit_code"], 1)
        self.assertEqual(body["error_kind"], "invalid_input")

    def test_systemone_non_input_error_returns_engine_error_record(self):
        server, _ = self._start_server(
            systemone_behavior=SystemOneError("timeout", "system one timeout")
        )
        status, body = self._request(
            server,
            "POST",
            "/v1/decide",
            body=self._valid_decide_payload(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "engine_error")
        self.assertEqual(body["exit_code"], 2)
        self.assertEqual(body["error_kind"], "timeout")

    def test_systemone_malformed_error_returns_invalid_response_with_safe_note(self):
        server, _ = self._start_server(
            systemone_behavior=SystemOneError("malformed", "raw provider payload leaked")
        )
        status, body = self._request(
            server,
            "POST",
            "/v1/decide",
            body=self._valid_decide_payload(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "invalid_response")
        self.assertEqual(body["exit_code"], 2)
        self.assertEqual(body["error_kind"], "malformed")
        self.assertEqual(body["notes"], ["System One returned a malformed response"])
        self.assertNotIn("raw provider payload leaked", json.dumps(body, ensure_ascii=False))

    def test_decide_engine_md1_routes_to_md1_engine(self):
        md1_decision = {
            "schema": "eljev.decision/1",
            "shape": "decide",
            "status": "selected",
            "exit_code": 0,
            "choice": "alpha",
        }
        server, _ = self._start_server(
            md1_endpoint="https://vcinn.services.ai.azure.com",
            md1_decide_behavior=md1_decision,
        )
        payload = self._valid_decide_payload()
        payload["engine"] = "md1"
        status, body = self._request(
            server,
            "POST",
            "/v1/decide",
            body=payload,
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["choice"], "alpha")
        self.assertEqual(len(server.md1.decide_calls), 1)
        self.assertEqual(len(server.systemone.calls), 0)

    def test_screen_engine_md1_routes_to_md1_screen(self):
        md1_screen = {
            "schema": "eljev.decision/1",
            "shape": "screen",
            "status": "selected",
            "exit_code": 0,
            "choice": "beta",
            "results": [{"id": "beta", "index": 1, "relevance_score": 0.9}],
            "tier_path": ["pregate", "md1"],
        }
        server, _ = self._start_server(
            md1_endpoint="https://vcinn.services.ai.azure.com",
            md1_screen_behavior=md1_screen,
        )
        status, body = self._request(
            server,
            "POST",
            "/v1/screen",
            body={
                "criterion": "pick best candidate",
                "candidates": [
                    {"id": "alpha", "text": "candidate alpha"},
                    {"id": "beta", "text": "candidate beta"},
                ],
                "engine": "md1",
                "top_n": 2,
            },
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["choice"], "beta")
        self.assertEqual(len(server.md1.screen_calls), 1)
        self.assertEqual(server.md1.screen_calls[0][0], "pick best candidate")

    def test_unexpected_screen_exception_returns_500_internal_without_traceback(self):
        server, _ = self._start_server(
            md1_endpoint="https://vcinn.services.ai.azure.com",
            md1_screen_behavior=RuntimeError("screen boom"),
        )
        status, body = self._request(
            server,
            "POST",
            "/v1/screen",
            body={
                "criterion": "pick best candidate",
                "candidates": [
                    {"id": "alpha", "text": "candidate alpha"},
                    {"id": "beta", "text": "candidate beta"},
                ],
                "engine": "md1",
                "top_n": 2,
            },
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 500)
        self.assertEqual(body["error_kind"], "internal")
        self.assertEqual(body["message"], "internal server error")
        self.assertNotIn("Traceback", json.dumps(body, ensure_ascii=False))

    def test_gate_endpoint_engine_md1_returns_gate_envelope(self):
        gate_result = {
            "schema": "eljev.gate/1",
            "session_id": "session-42",
            "engine": "Decision-1",
            "status": "selected",
            "exit_code": 0,
            "tier": "fast_direct",
            "intent": "code_modification",
            "needs_deep_analysis": 0.05,
            "enforceable": True,
            "extension_mode": "enforce",
            "coverage_policy": "calibrated",
            "budget_text": "[gate]",
            "decisions": {},
            "elapsed_ms": 1.5,
            "error_kind": None,
            "notes": [],
        }
        server, _ = self._start_server(
            md1_endpoint="https://vcinn.services.ai.azure.com",
            md1_gate_behavior=gate_result,
        )
        status, body = self._request(
            server,
            "POST",
            "/v1/gate",
            body={"state": "Please implement this", "engine": "md1", "session_id": "session-42"},
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["schema"], "eljev.gate/1")
        self.assertEqual(body["session_id"], "session-42")
        self.assertTrue(body["enforceable"])
        self.assertEqual(len(server.md1.gate_calls), 1)

    def test_gate_rejects_state_longer_than_max_chars_with_exit_code_one(self):
        server, _ = self._start_server(md1_endpoint="https://vcinn.services.ai.azure.com")
        too_large_state = "x" * 100_001
        status, body = self._request(
            server,
            "POST",
            "/v1/gate",
            body={"state": too_large_state, "engine": "md1", "session_id": "session-too-large"},
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        self.assertEqual(body["error_kind"], "invalid_input")
        self.assertEqual(body["exit_code"], 1)

    def test_gate_endpoint_maps_systemone_malformed_to_invalid_response_with_safe_note(self):
        server, _ = self._start_server(
            systemone_behavior=SystemOneError("malformed", "raw provider details")
        )
        status, body = self._request(
            server,
            "POST",
            "/v1/gate",
            body={"state": "Investigate this request", "engine": "cohere", "session_id": "s-cohere"},
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["schema"], "eljev.gate/1")
        self.assertEqual(body["status"], "invalid_response")
        self.assertEqual(body["error_kind"], "malformed")
        self.assertEqual(body["notes"], ["System One returned a malformed response"])
        self.assertNotIn("raw provider details", json.dumps(body, ensure_ascii=False))

    def test_unexpected_systemone_exception_returns_500_internal_without_traceback(self):
        server, _ = self._start_server(systemone_behavior=RuntimeError("boom"))
        status, body = self._request(
            server,
            "POST",
            "/v1/decide",
            body=self._valid_decide_payload(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 500)
        self.assertEqual(body["error_kind"], "internal")
        body_text = json.dumps(body, ensure_ascii=False)
        self.assertNotIn("Traceback", body_text)
        self.assertEqual(body["message"], "internal server error")

    def test_successful_decide_appends_to_decision_log(self):
        server, _ = self._start_server()
        status, _ = self._request(
            server,
            "POST",
            "/v1/decide",
            body=self._valid_decide_payload(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)

        decisions_path = Path(self._tempdir.name) / "logs" / "decisions.jsonl"
        self.assertTrue(decisions_path.exists())
        lines = decisions_path.read_text(encoding="utf-8").splitlines()
        self.assertGreaterEqual(len(lines), 1)
        decoded = json.loads(lines[-1])
        self.assertEqual(decoded["shape"], "decide")
        self.assertEqual(decoded["choice"], "alpha")

    def test_logging_off_does_not_append_to_decision_log(self):
        os.environ["ELJEV_LOGGING"] = "OFF"
        server, _ = self._start_server()
        status, _ = self._request(
            server,
            "POST",
            "/v1/decide",
            body=self._valid_decide_payload(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        decisions_path = Path(self._tempdir.name) / "logs" / "decisions.jsonl"
        self.assertFalse(decisions_path.exists())

    def test_shutdown_returns_stopping_and_serve_thread_exits(self):
        server, thread = self._start_server()
        status, body = self._request(
            server,
            "POST",
            "/v1/shutdown",
            body={},
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "stopping")
        self.assertEqual(body["pid"], os.getpid())
        thread.join(timeout=3.0)
        self.assertFalse(thread.is_alive())

    @unittest.skipUnless(os.name == "nt", "exclusive bind behavior is only applicable on Windows")
    def test_windows_second_bind_on_same_port_fails(self):
        first = daemon.EljevHTTPServer(("127.0.0.1", 0), daemon.EljevRequestHandler)
        port = first.server_port
        try:
            with self.assertRaises(OSError):
                daemon.EljevHTTPServer(("127.0.0.1", port), daemon.EljevRequestHandler)
        finally:
            first.server_close()

    def test_run_writes_pidfile_then_removes_it_on_shutdown(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            run_port = probe.getsockname()[1]
        os.environ["ELJEV_PORT"] = str(run_port)

        result_holder: list[int] = []
        pid_path = Path(self._tempdir.name) / "daemon.pid"

        def run_target() -> None:
            result_holder.append(daemon.run())

        with patch("eljev.daemon.CohereClient", _FakeRunCohere), patch(
            "eljev.daemon.signal.signal", lambda *args, **kwargs: None
        ):
            thread = threading.Thread(target=run_target, daemon=True)
            thread.start()
            deadline = time.time() + 3.0
            while time.time() < deadline and not pid_path.exists():
                time.sleep(0.02)
            self.assertTrue(pid_path.exists())
            record = json.loads(pid_path.read_text(encoding="utf-8"))
            self.assertEqual(record["pid"], os.getpid())
            self.assertEqual(record["port"], run_port)
            self.assertIsInstance(record["started_at"], float)

            conn = http.client.HTTPConnection("127.0.0.1", run_port, timeout=2.0)
            try:
                conn.request(
                    "POST",
                    "/v1/shutdown",
                    body=b"{}",
                    headers={"Content-Type": "application/json"},
                )
                response = conn.getresponse()
                response.read()
                self.assertEqual(response.status, 200)
            finally:
                conn.close()

            thread.join(timeout=3.0)
            self.assertFalse(thread.is_alive())

        self.assertEqual(result_holder, [0])
        self.assertFalse(pid_path.exists())

    def test_run_returns_3_when_port_is_already_bound(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as bound:
            bound.bind(("127.0.0.1", 0))
            bound.listen(1)
            os.environ["ELJEV_PORT"] = str(bound.getsockname()[1])
            self.assertEqual(daemon.run(), 3)


if __name__ == "__main__":
    unittest.main()
