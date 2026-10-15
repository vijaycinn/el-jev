import http.client
import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from eljev.engines.auth import FoundryAuthError, FoundryBearerProvider
from eljev.engines.md1 import MD1_PATH, MD1Client, MD1Error


class _FakeHTTPResponse:
    def __init__(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self._body = body
        self._headers = {key.lower(): value for key, value in (headers or {}).items()}

    def read(self) -> bytes:
        return self._body

    def getheader(self, name: str, default=None):  # noqa: ANN001
        return self._headers.get(name.lower(), default)


class _FakeConnection:
    def __init__(self, scripted: list[object], *, sock=object()) -> None:
        self.scripted = list(scripted)
        self.timeout = None
        self.sock = sock
        self.closed = False
        self.request_calls: list[tuple[str, str, bytes | None, dict[str, str] | None]] = []

    def request(self, method: str, path: str, body=None, headers=None) -> None:
        self.request_calls.append((method, path, body, headers))

    def getresponse(self):
        if not self.scripted:
            raise AssertionError("no scripted response left")
        action = self.scripted.pop(0)
        if isinstance(action, BaseException):
            raise action
        return action

    def close(self) -> None:
        self.closed = True


class _ConnectionFactory:
    def __init__(self, connections: list[_FakeConnection]) -> None:
        self.connections = list(connections)
        self.calls: list[tuple[str, int | None, float]] = []

    def __call__(self, host: str, *, port: int | None = None, timeout: float = 0.0):
        self.calls.append((host, port, timeout))
        if not self.connections:
            raise AssertionError("factory exhausted")
        return self.connections.pop(0)


class MD1ClientTests(unittest.TestCase):
    ENV_KEYS = (
        "ELJEV_DIR",
        "ELJEV_MD1_ENDPOINT",
        "ELJEV_MD1_DEPLOYMENT",
        "ELJEV_MD1_TIMEOUT_MS",
        "ELJEV_TIMEOUT_MS",
        "ELJEV_COVERAGE_POLICY",
        "ELJEV_ENGINE",
    )

    def setUp(self) -> None:
        self._environment = patch.dict(os.environ, {}, clear=False)
        self._environment.start()
        self._tempdir = tempfile.TemporaryDirectory()
        for key in self.ENV_KEYS:
            os.environ.pop(key, None)
        os.environ["ELJEV_DIR"] = self._tempdir.name

    def tearDown(self) -> None:
        self._environment.stop()
        self._tempdir.cleanup()

    @staticmethod
    def _choice_question() -> dict[str, dict[str, object]]:
        return {
            "q1": {
                "type": "choice",
                "criteria": {
                    "alpha": "Alpha queue",
                    "beta": "Beta queue",
                },
            }
        }

    @staticmethod
    def _choice_success_response() -> _FakeHTTPResponse:
        return _FakeHTTPResponse(
            200,
            json.dumps(
                {
                    "answers": {
                        "q1": {
                            "choice": "alpha",
                            "confidence": 0.9,
                            "probabilities": {"alpha": 0.9, "beta": 0.1},
                        }
                    }
                },
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8"),
        )

    def test_ask_posts_md1_path_with_model_and_questions(self):
        connection = _FakeConnection([self._choice_success_response()])
        factory = _ConnectionFactory([connection])
        client = MD1Client(
            endpoint="https://example.services.ai.azure.com",
            deployment="Decision-1",
            timeout_ms=1000,
            connection_factory=factory,
            token_provider=lambda: "token",
        )

        result = client.ask("state payload", self._choice_question())

        self.assertEqual(result["answers"]["q1"]["choice"], "alpha")
        self.assertEqual(len(connection.request_calls), 1)
        method, path, body, headers = connection.request_calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(path, MD1_PATH)
        decoded = json.loads(body.decode("utf-8"))
        self.assertEqual(decoded["model"], "Decision-1")
        self.assertEqual(decoded["questions"], self._choice_question())
        self.assertEqual(decoded["state"], "state payload")
        self.assertIsInstance(headers, dict)
        self.assertEqual(headers.get("Content-Type"), "application/json")
        self.assertIn("Authorization", headers)
        self.assertTrue(str(headers["Authorization"]).strip())

    def test_bare_and_full_endpoint_use_same_host_and_md1_path(self):
        endpoints = (
            "https://example.services.ai.azure.com",
            f"https://example.services.ai.azure.com{MD1_PATH}",
        )
        for endpoint in endpoints:
            with self.subTest(endpoint=endpoint):
                connection = _FakeConnection([self._choice_success_response()])
                factory = _ConnectionFactory([connection])
                client = MD1Client(
                    endpoint=endpoint,
                    timeout_ms=1000,
                    connection_factory=factory,
                    token_provider=lambda: "token",
                )

                client.ask("state", self._choice_question())

                self.assertEqual(len(factory.calls), 1)
                host, _port, _timeout = factory.calls[0]
                self.assertEqual(host, "example.services.ai.azure.com")
                self.assertEqual(len(connection.request_calls), 1)
                self.assertEqual(connection.request_calls[0][1], MD1_PATH)

    def test_non_foundry_host_raises_http_without_sending_request(self):
        secret_token = "super-secret-token"
        connection_factory_called = {"called": False}

        def forbidden_connection_factory(*args, **kwargs):  # noqa: ANN001, ANN002, ANN003
            connection_factory_called["called"] = True
            raise AssertionError("connection factory must not be called")

        client = MD1Client(
            endpoint="https://evil.example.com",
            timeout_ms=1000,
            connection_factory=forbidden_connection_factory,
            token_provider=lambda: secret_token,
        )

        with self.assertRaises(MD1Error) as raised:
            client.ask("state", self._choice_question())

        self.assertEqual(raised.exception.error_kind, "http")
        self.assertFalse(connection_factory_called["called"])
        self.assertNotIn(secret_token, str(raised.exception))

    def test_stale_reused_connection_retries_once_on_fresh_connection(self):
        stale = _FakeConnection([http.client.RemoteDisconnected("stale-connection")])
        fresh = _FakeConnection([self._choice_success_response()])
        factory = _ConnectionFactory([fresh])
        client = MD1Client(
            endpoint="https://example.services.ai.azure.com",
            timeout_ms=1000,
            connection_factory=factory,
            token_provider=lambda: "token",
        )
        client._connection = stale

        result = client.ask("state", self._choice_question())

        self.assertEqual(result["answers"]["q1"]["choice"], "alpha")
        self.assertEqual(len(stale.request_calls), 1)
        self.assertTrue(stale.closed)
        self.assertEqual(len(factory.calls), 1)
        self.assertEqual(len(fresh.request_calls), 1)

    def test_fresh_connection_disconnect_raises_connection_error(self):
        failing = _FakeConnection([http.client.RemoteDisconnected("fresh disconnect")])
        factory = _ConnectionFactory([failing])
        secret_token = "secret-md1-token"
        client = MD1Client(
            endpoint="https://example.services.ai.azure.com",
            timeout_ms=1000,
            connection_factory=factory,
            token_provider=lambda: secret_token,
        )

        with self.assertRaises(MD1Error) as raised:
            client.ask("state", self._choice_question())

        self.assertEqual(raised.exception.error_kind, "connection")
        self.assertNotIn(secret_token, str(raised.exception))

    def test_429_with_retry_after_zero_retries_then_succeeds(self):
        connection = _FakeConnection(
            [
                _FakeHTTPResponse(429, b"{}", headers={"Retry-After": "0"}),
                self._choice_success_response(),
            ]
        )
        factory = _ConnectionFactory([connection])
        client = MD1Client(
            endpoint="https://example.services.ai.azure.com",
            timeout_ms=1000,
            connection_factory=factory,
            token_provider=lambda: "token",
        )

        with patch("eljev.engines.md1.time.sleep") as sleep_mock:
            result = client.ask("state", self._choice_question())

        self.assertEqual(result["answers"]["q1"]["choice"], "alpha")
        self.assertGreaterEqual(sleep_mock.call_count, 1)
        self.assertEqual(len(connection.request_calls), 2)

    def test_401_raises_auth_and_404_raises_http_without_leaking_secret(self):
        cases = [
            ("401-auth", 401, "auth"),
            ("404-deployment", 404, "http"),
        ]
        for case_name, status_code, expected_kind in cases:
            with self.subTest(case=case_name):
                connection = _FakeConnection([_FakeHTTPResponse(status_code, b"{}")])
                factory = _ConnectionFactory([connection])
                secret_token = "very-secret-token-value"
                client = MD1Client(
                    endpoint="https://example.services.ai.azure.com",
                    timeout_ms=1000,
                    connection_factory=factory,
                    token_provider=lambda: secret_token,
                )

                with self.assertRaises(MD1Error) as raised:
                    client.ask("state", self._choice_question())

                self.assertEqual(raised.exception.error_kind, expected_kind)
                self.assertNotIn(secret_token, str(raised.exception))

    def test_ask_wraps_foundry_auth_error_from_token_provider(self):
        client = MD1Client(
            endpoint="https://example.services.ai.azure.com",
            timeout_ms=1000,
            token_provider=lambda: (_ for _ in ()).throw(FoundryAuthError("auth", "token unavailable")),
        )

        with self.assertRaises(MD1Error) as raised:
            client.ask("state", self._choice_question())

        self.assertEqual(raised.exception.error_kind, "auth")
        self.assertIn("token unavailable", str(raised.exception))

    def test_bearer_provider_serves_unexpired_token_while_lock_is_held(self):
        provider = FoundryBearerProvider(refresh_skew_s=300.0)
        provider._token = "cached-token"  # noqa: SLF001
        provider._token_expiry_epoch = time.time() + 60.0  # noqa: SLF001
        provider._lock.acquire()  # noqa: SLF001
        try:
            with patch.object(provider, "_mint_token", side_effect=AssertionError("must not mint")):
                started = time.perf_counter()
                token = provider.get_token()
                elapsed = time.perf_counter() - started
        finally:
            provider._lock.release()  # noqa: SLF001

        self.assertEqual(token, "cached-token")
        self.assertLess(elapsed, 0.2)


if __name__ == "__main__":
    unittest.main()
