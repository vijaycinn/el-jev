from datetime import datetime
import http.client
import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from eljev.engines.cohere import CohereClient, CohereError


class _FakeHTTPResponse:
    def __init__(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.status = status
        self._body = body
        self._headers = {k.lower(): v for k, v in (headers or {}).items()}

    def read(self) -> bytes:
        return self._body

    def getheader(self, name: str, default=None):
        return self._headers.get(name.lower(), default)


class _FakeConnection:
    def __init__(self, scripted: list[object], *, sock=object()) -> None:
        self.scripted = list(scripted)
        self.timeout = None
        self.sock = sock
        self.request_calls: list[tuple[str, str, bytes | None, dict[str, str] | None]] = []
        self.connect_calls = 0
        self.closed = False

    def request(self, method: str, path: str, body=None, headers=None) -> None:
        self.request_calls.append((method, path, body, headers))

    def getresponse(self):
        if not self.scripted:
            raise AssertionError("no scripted response left")
        action = self.scripted.pop(0)
        if isinstance(action, BaseException):
            raise action
        return action

    def connect(self) -> None:
        self.connect_calls += 1
        if self.sock is None:
            self.sock = object()

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


class _UrlOpenResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return json.dumps(self._payload, ensure_ascii=False, allow_nan=False).encode("utf-8")


class CohereClientTests(unittest.TestCase):
    ENV_KEYS = (
        "ELJEV_DIR",
        "ELJEV_COVERAGE_POLICY",
        "ELJEV_CALIBRATION_PATH",
        "ELJEV_COHERE_ENDPOINT",
        "ELJEV_SYSTEMONE_URL",
        "ELJEV_AUTH",
        "EL_JEV",
        "ELJEV_PORT",
        "IDENTITY_ENDPOINT",
        "IDENTITY_HEADER",
        "AZURE_CLIENT_ID",
    )

    def setUp(self) -> None:
        self._environment = patch.dict(os.environ, {}, clear=False)
        self._environment.start()
        self._tempdir = tempfile.TemporaryDirectory()
        self._clear_relevant_env()
        os.environ["ELJEV_DIR"] = self._tempdir.name

    def tearDown(self) -> None:
        self._environment.stop()
        self._tempdir.cleanup()

    def _clear_relevant_env(self) -> None:
        for key in self.ENV_KEYS:
            os.environ.pop(key, None)

    def test_az_token_caching_and_refresh_skew(self):
        completed_1 = subprocess.CompletedProcess(
            args=["az"],
            returncode=0,
            stdout=json.dumps({"accessToken": "tok-1", "expires_on": 1000}),
            stderr="",
        )
        completed_2 = subprocess.CompletedProcess(
            args=["az"],
            returncode=0,
            stdout=json.dumps({"accessToken": "tok-2", "expires_on": 2000}),
            stderr="",
        )
        client = CohereClient(endpoint="https://example.services.ai.azure.com")
        with patch("eljev.engines.cohere.subprocess.run", side_effect=[completed_1, completed_2]) as run_mock, patch(
            "eljev.engines.cohere.shutil.which", return_value="az"
        ), patch(
            "eljev.engines.cohere.time.time",
            side_effect=[100.0, 100.0, 200.0, 750.0, 750.0],
        ):
            token_1 = client._get_token()
            token_2 = client._get_token()
            token_3 = client._get_token()

        self.assertEqual(token_1, "tok-1")
        self.assertEqual(token_2, "tok-1")
        self.assertEqual(token_3, "tok-2")
        self.assertEqual(run_mock.call_count, 2)

    def test_expires_on_naive_local_time_is_parsed_as_local_epoch(self):
        epoch = 2_000_000_000
        local_text = datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S.%f")
        parsed = CohereClient._parse_expiry_epoch({"expiresOn": local_text})
        self.assertIsNotNone(parsed)
        self.assertLess(abs(float(parsed) - float(epoch)), 1.0)

    def test_az_non_zero_exit_raises_auth_without_token_leak(self):
        completed = subprocess.CompletedProcess(
            args=["az"],
            returncode=1,
            stdout=json.dumps({"accessToken": "secret-token"}),
            stderr="denied secret-token",
        )
        client = CohereClient(endpoint="https://example.services.ai.azure.com")
        with patch("eljev.engines.cohere.subprocess.run", return_value=completed), patch(
            "eljev.engines.cohere.shutil.which", return_value="az"
        ):
            with self.assertRaises(CohereError) as raised:
                client._mint_azcli_token()
        self.assertEqual(raised.exception.error_kind, "auth")
        self.assertNotIn("secret-token", str(raised.exception))

    def test_managed_identity_uses_imds_with_metadata_header_and_client_id(self):
        os.environ["ELJEV_AUTH"] = "managed_identity"
        os.environ["AZURE_CLIENT_ID"] = "client-guid"
        captured: dict[str, object] = {}

        def fake_urlopen(request, timeout):  # noqa: ANN001
            captured["url"] = request.full_url
            captured["headers"] = {k.lower(): v for k, v in request.header_items()}
            captured["timeout"] = timeout
            return _UrlOpenResponse({"access_token": "tok", "expires_on": 4_000_000_000})

        client = CohereClient(endpoint="https://example.services.ai.azure.com")
        with patch("eljev.engines.cohere.urlopen", side_effect=fake_urlopen):
            token, expiry = client._mint_managed_identity_token()

        self.assertEqual(token, "tok")
        self.assertIsNotNone(expiry)
        self.assertIn("http://169.254.169.254/metadata/identity/oauth2/token", captured["url"])
        self.assertIn("client_id=client-guid", captured["url"])
        self.assertEqual(captured["headers"]["metadata"], "true")

    def test_managed_identity_uses_app_service_endpoint_when_present(self):
        os.environ["ELJEV_AUTH"] = "managed_identity"
        os.environ["IDENTITY_ENDPOINT"] = "http://127.0.0.1:41741/msi/token"
        os.environ["IDENTITY_HEADER"] = "identity-secret"
        os.environ["AZURE_CLIENT_ID"] = "client-guid"
        captured: dict[str, object] = {}

        def fake_urlopen(request, timeout):  # noqa: ANN001
            captured["url"] = request.full_url
            captured["headers"] = {k.lower(): v for k, v in request.header_items()}
            captured["timeout"] = timeout
            return _UrlOpenResponse({"access_token": "tok-app", "expires_on": 4_000_000_000})

        client = CohereClient(endpoint="https://example.services.ai.azure.com")
        with patch("eljev.engines.cohere.urlopen", side_effect=fake_urlopen):
            token, _ = client._mint_managed_identity_token()

        self.assertEqual(token, "tok-app")
        self.assertIn("http://127.0.0.1:41741/msi/token?", captured["url"])
        self.assertIn("api-version=2019-08-01", captured["url"])
        self.assertIn("client_id=client-guid", captured["url"])
        self.assertEqual(captured["headers"]["x-identity-header"], "identity-secret")

    def test_managed_identity_failure_raises_auth(self):
        os.environ["ELJEV_AUTH"] = "managed_identity"
        client = CohereClient(endpoint="https://example.services.ai.azure.com")
        with patch("eljev.engines.cohere.urlopen", side_effect=OSError("network down")):
            with self.assertRaises(CohereError) as raised:
                client._mint_managed_identity_token()
        self.assertEqual(raised.exception.error_kind, "auth")

    def test_rerank_retries_once_for_stale_reused_connection(self):
        stale = _FakeConnection([http.client.RemoteDisconnected("stale")])
        fresh = _FakeConnection(
            [
                _FakeHTTPResponse(
                    200,
                    json.dumps({"results": [{"index": 0, "relevance_score": 0.9}]}).encode("utf-8"),
                )
            ]
        )
        factory = _ConnectionFactory([fresh])
        client = CohereClient(
            endpoint="https://example.services.ai.azure.com",
            connection_factory=factory,
            token_provider=lambda: "tok",
            timeout_ms=1000,
        )
        client._connection = stale

        result = client.rerank("criterion", ["doc-1"], top_n=1)

        self.assertEqual(result["results"][0]["index"], 0)
        self.assertEqual(len(stale.request_calls), 1)
        self.assertEqual(len(fresh.request_calls), 1)
        self.assertEqual(len(factory.calls), 1)

    def test_rerank_does_not_retry_remote_disconnected_on_fresh_connection(self):
        fresh = _FakeConnection([http.client.RemoteDisconnected("fresh-fail")])
        factory = _ConnectionFactory([fresh])
        client = CohereClient(
            endpoint="https://example.services.ai.azure.com",
            connection_factory=factory,
            token_provider=lambda: "tok",
            timeout_ms=1000,
        )
        with self.assertRaises(CohereError) as raised:
            client.rerank("criterion", ["doc-1"], top_n=1)
        self.assertEqual(raised.exception.error_kind, "connection")
        self.assertEqual(len(factory.calls), 1)

    def test_rerank_retries_429_with_retry_after_zero_then_succeeds(self):
        connection = _FakeConnection(
            [
                _FakeHTTPResponse(429, b"{}", headers={"Retry-After": "0"}),
                _FakeHTTPResponse(
                    200,
                    json.dumps({"results": [{"index": 0, "relevance_score": 0.8}]}).encode("utf-8"),
                ),
            ]
        )
        factory = _ConnectionFactory([connection])
        client = CohereClient(
            endpoint="https://example.services.ai.azure.com",
            connection_factory=factory,
            token_provider=lambda: "tok",
            timeout_ms=1000,
        )
        with patch("eljev.engines.cohere.time.sleep") as sleep_mock:
            result = client.rerank("criterion", ["doc-1"], top_n=1)

        self.assertEqual(result["results"][0]["index"], 0)
        self.assertGreaterEqual(sleep_mock.call_count, 1)

    def test_rerank_401_raises_auth(self):
        connection = _FakeConnection([_FakeHTTPResponse(401, b"{}")])
        factory = _ConnectionFactory([connection])
        client = CohereClient(
            endpoint="https://example.services.ai.azure.com",
            connection_factory=factory,
            token_provider=lambda: "tok",
            timeout_ms=1000,
        )
        with self.assertRaises(CohereError) as raised:
            client.rerank("criterion", ["doc-1"], top_n=1)
        self.assertEqual(raised.exception.error_kind, "auth")

    def test_authorization_value_is_not_leaked_in_errors(self):
        secret = "super-secret-token"
        connection = _FakeConnection([http.client.RemoteDisconnected("network fail")])
        factory = _ConnectionFactory([connection])
        client = CohereClient(
            endpoint="https://example.services.ai.azure.com",
            connection_factory=factory,
            token_provider=lambda: secret,
            timeout_ms=1000,
        )
        with self.assertRaises(CohereError) as raised:
            client.rerank("criterion", ["doc-1"], top_n=1)
        self.assertNotIn(secret, str(raised.exception))

    def test_warm_returns_false_without_endpoint_and_skips_token_provider(self):
        calls = {"token_provider": 0}

        def provider() -> str:
            calls["token_provider"] += 1
            return "tok"

        client = CohereClient(endpoint="", token_provider=provider)
        self.assertFalse(client.warm())
        self.assertEqual(calls["token_provider"], 0)

    def test_warm_connects_only_when_socket_is_none(self):
        connection = _FakeConnection([], sock=None)
        factory = _ConnectionFactory([connection])
        client = CohereClient(
            endpoint="https://example.services.ai.azure.com",
            connection_factory=factory,
            token_provider=lambda: "tok",
            timeout_ms=1000,
        )

        self.assertTrue(client.warm())
        self.assertTrue(client.warm())
        self.assertEqual(connection.connect_calls, 1)


if __name__ == "__main__":
    unittest.main()
