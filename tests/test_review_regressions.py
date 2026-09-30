"""Regression tests for the v0.2.0 critic findings (C1, C2, I3, I4, I6, M2)."""

import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from eljev import cli
from eljev.engines.cohere import CohereClient, CohereError
from eljev.engines.systemone import SystemOneEngine


def _closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class _IsolatedEnv(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory()
        self._env = patch.dict(os.environ, {}, clear=False)
        self._env.start()
        for key in ("EL_JEV", "ELJEV_ENABLED", "ELJEV_COVERAGE_POLICY", "ELJEV_CALIBRATION_PATH", "ELJEV_SYSTEMONE_URL"):
            os.environ.pop(key, None)
        os.environ["ELJEV_DIR"] = self._tempdir.name
        os.environ["ELJEV_PORT"] = str(_closed_port())
        self._user_env = patch("eljev.config.user_env_value", return_value=None)
        self._user_env.start()

    def tearDown(self) -> None:
        self._user_env.stop()
        self._env.stop()
        self._tempdir.cleanup()


class _PartialCohere:
    endpoint = "https://example.services.ai.azure.com"
    deployment = "fake-deploy"

    def __init__(self, results):
        self._results = results

    def rerank(self, query, documents, top_n):
        return {"results": self._results}


class TruncatedProviderResponseTests(_IsolatedEnv):
    """C1: a partial ranking must never be zero-filled into a confident decision."""

    def _calibrated(self) -> None:
        path = Path(self._tempdir.name) / "calibration.json"
        params = {"calibration_version": "t", "temperature": 0.15, "threshold": 0.6, "margin_threshold": 0.2}
        path.write_text(json.dumps({"kinds": {"choice": params}}), encoding="utf-8")
        os.environ["ELJEV_CALIBRATION_PATH"] = str(path)
        os.environ["ELJEV_COVERAGE_POLICY"] = "calibrated"

    def _question(self):
        return {"id": "q", "kind": "choice", "options": ["alpha", "bravo", "charlie", "delta"]}

    def test_partial_ranking_is_invalid_response_not_selected(self):
        self._calibrated()
        engine = SystemOneEngine(cohere=_PartialCohere([{"index": 0, "relevance_score": 0.51}]))
        record = engine.decide("state text", self._question())
        self.assertEqual(record["status"], "invalid_response")
        self.assertEqual(record["exit_code"], 2)
        self.assertIsNone(record["choice"])
        self.assertIsNone(record["probabilities"])

    def test_complete_bunched_ranking_still_needs_review(self):
        self._calibrated()
        results = [{"index": i, "relevance_score": s} for i, s in enumerate((0.51, 0.50, 0.49, 0.48))]
        record = SystemOneEngine(cohere=_PartialCohere(results)).decide("state text", self._question())
        self.assertEqual(record["status"], "needs_review")
        self.assertEqual(record["exit_code"], 2)


class StalePidfileSafetyTests(_IsolatedEnv):
    """C2: `daemon stop` must never terminate a live process it cannot prove is el-jev."""

    def _run_stop(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.main(["daemon", "stop"])
        return code, json.loads(output.getvalue())

    def _spawn_bystander(self) -> subprocess.Popen:
        return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])

    def test_legacy_plain_pidfile_for_live_unrelated_process_is_not_killed(self):
        bystander = self._spawn_bystander()
        try:
            Path(self._tempdir.name, "daemon.pid").write_text(f"{bystander.pid}\n", encoding="ascii")
            code, result = self._run_stop()
            self.assertEqual(code, 0)
            self.assertTrue(result.get("stale_pidfile"))
            self.assertIsNone(bystander.poll(), "unrelated process must still be running")
            self.assertFalse(Path(self._tempdir.name, "daemon.pid").exists())
        finally:
            bystander.kill()
            bystander.wait()

    def test_json_pidfile_older_than_process_is_treated_as_recycled_pid(self):
        bystander = self._spawn_bystander()
        try:
            # Pidfile claims the daemon started an hour before this process existed.
            record = {"pid": bystander.pid, "port": 1, "started_at": time.time() - 3600, "exe": sys.executable}
            Path(self._tempdir.name, "daemon.pid").write_text(json.dumps(record), encoding="utf-8")
            code, result = self._run_stop()
            self.assertEqual(code, 0)
            self.assertTrue(result.get("stale_pidfile"))
            self.assertIsNone(bystander.poll(), "recycled pid must not be terminated")
        finally:
            bystander.kill()
            bystander.wait()

    @unittest.skipUnless(sys.platform == "win32" or sys.platform.startswith("linux"), "needs process start time")
    def test_json_pidfile_written_after_process_start_is_owned(self):
        bystander = self._spawn_bystander()
        try:
            time.sleep(0.2)
            record = {"pid": bystander.pid, "port": 1, "started_at": time.time(), "exe": sys.executable}
            self.assertTrue(cli._pidfile_owns_process(record))
        finally:
            bystander.kill()
            bystander.wait()

    def test_pidfile_record_reads_json_and_legacy_formats(self):
        path = Path(self._tempdir.name, "daemon.pid")
        path.write_text(json.dumps({"pid": 42, "started_at": 10.5}), encoding="utf-8")
        self.assertEqual(cli._read_pidfile_record(path), {"pid": 42, "started_at": 10.5})
        path.write_text("43\n", encoding="ascii")
        self.assertEqual(cli._read_pidfile_record(path), {"pid": 43, "started_at": None})
        path.write_text("garbage", encoding="ascii")
        self.assertIsNone(cli._read_pidfile_record(path))


class TokenRefreshDoesNotBlockTests(unittest.TestCase):
    """I3: a request must not wait on `az` while the cached token is still usable."""

    def test_valid_token_is_served_while_refresh_is_in_flight(self):
        release = threading.Event()
        calls = []

        def slow_provider():
            calls.append(1)
            if len(calls) > 1:
                release.wait(5)
            return f"tok-{len(calls)}"

        client = CohereClient(endpoint="https://example.services.ai.azure.com", token_provider=slow_provider)
        client._get_token()
        # Inside the refresh skew window but not yet expired.
        client._token_expiry_epoch = time.time() + 60
        refresher = threading.Thread(target=client._get_token, daemon=True)
        refresher.start()
        time.sleep(0.1)
        started = time.perf_counter()
        token = client._get_token()
        elapsed = time.perf_counter() - started
        release.set()
        refresher.join(5)
        self.assertEqual(token, "tok-1")
        self.assertLess(elapsed, 0.2)


class WarmDoesNotBlockRerankTests(unittest.TestCase):
    """I4: warm-up must not take the connection lock while a request holds it."""

    def test_warm_skips_when_connection_is_busy(self):
        client = CohereClient(endpoint="https://example.services.ai.azure.com", token_provider=lambda: "tok")
        client._connection_lock.acquire()
        try:
            started = time.perf_counter()
            self.assertTrue(client.warm())
            self.assertLess(time.perf_counter() - started, 0.2)
        finally:
            client._connection_lock.release()


class _SocketConnection:
    def __init__(self, sock):
        self.sock = sock
        self.connect_calls = 0

    def connect(self):
        self.connect_calls += 1
        self.sock = object()

    def close(self):
        self.sock = None


class WarmReconnectsServerClosedIdleConnectionTests(unittest.TestCase):
    """An idle keep-alive the server already closed is replaced in the background."""

    def test_server_closed_socket_is_replaced(self):
        ours, theirs = socket.socketpair()
        theirs.close()  # the server side hung up: our end becomes readable (EOF)
        stale = _SocketConnection(ours)
        fresh = _SocketConnection(None)
        made = []

        def factory(*args, **kwargs):
            made.append(1)
            return fresh

        client = CohereClient(
            endpoint="https://example.services.ai.azure.com", token_provider=lambda: "tok", connection_factory=factory
        )
        client._connection = stale
        try:
            self.assertTrue(client.warm())
            self.assertIs(client._connection, fresh)
            self.assertEqual(fresh.connect_calls, 1)
            self.assertEqual(len(made), 1)
        finally:
            ours.close()

    def test_healthy_idle_socket_is_kept(self):
        ours, theirs = socket.socketpair()
        live = _SocketConnection(ours)
        client = CohereClient(
            endpoint="https://example.services.ai.azure.com",
            token_provider=lambda: "tok",
            connection_factory=lambda *a, **k: self.fail("must not reconnect a healthy idle socket"),
        )
        client._connection = live
        try:
            self.assertTrue(client.warm())
            self.assertIs(client._connection, live)
            self.assertEqual(live.connect_calls, 0)
        finally:
            ours.close()
            theirs.close()


class _Response:
    def __init__(self, status):
        self.status = status

    def read(self):
        return b'{"error":"busy"}'

    def getheader(self, name):
        return "0" if name == "Retry-After" else None


class _AlwaysBusyConnection:
    sock = object()

    def __init__(self):
        self.requests = 0

    def request(self, *args, **kwargs):
        self.requests += 1

    def getresponse(self):
        return _Response(503)

    def close(self):
        return None


class RetryCapTests(unittest.TestCase):
    """M2: server-error retries are capped and do not hold the connection lock while sleeping."""

    def test_503_retries_are_capped(self):
        connection = _AlwaysBusyConnection()
        client = CohereClient(
            endpoint="https://example.services.ai.azure.com",
            timeout_ms=10_000,
            token_provider=lambda: "tok",
            connection_factory=lambda *a, **k: connection,
        )
        with self.assertRaises(CohereError) as caught:
            client.rerank("q", ["a", "b"], 2)
        self.assertEqual(caught.exception.error_kind, "http")
        self.assertEqual(connection.requests, 4)


class AzureSubscriptionPinTests(_IsolatedEnv):
    """az CLI tokens must be minted by the subscription's account, not the az default account."""

    SUBSCRIPTION = "00000000-1111-2222-3333-444444444444"

    def _mint_args(self) -> list[str]:
        completed = subprocess.CompletedProcess(
            args=["az"], returncode=0, stdout=json.dumps({"accessToken": "tok", "expires_on": 9_999_999_999}), stderr=""
        )
        client = CohereClient(endpoint="https://example.services.ai.azure.com")
        with patch("eljev.engines.cohere.subprocess.run", return_value=completed) as run_mock, patch(
            "eljev.engines.cohere.shutil.which", return_value="az"
        ):
            client._get_token()
        return list(run_mock.call_args.args[0])

    def test_no_subscription_uses_az_default(self):
        os.environ.pop("ELJEV_AZURE_SUBSCRIPTION", None)
        self.assertNotIn("--subscription", self._mint_args())

    def test_env_subscription_is_passed_to_az(self):
        os.environ["ELJEV_AZURE_SUBSCRIPTION"] = self.SUBSCRIPTION
        args = self._mint_args()
        self.assertEqual(args[args.index("--subscription") + 1], self.SUBSCRIPTION)
        self.assertEqual(args[:3], ["az", "account", "get-access-token"])
        self.assertNotIn("--tenant", args)

    def test_configure_subscription_validates_persists_and_clears(self):
        os.environ.pop("ELJEV_AZURE_SUBSCRIPTION", None)

        def run(arguments):
            output = io.StringIO()
            with redirect_stdout(output):
                code = cli.main(arguments)
            return code, json.loads(output.getvalue())

        code, _ = run(["configure", "--subscription", "not-a-guid"])
        self.assertEqual(code, 1)
        code, result = run(["configure", "--subscription", self.SUBSCRIPTION])
        self.assertEqual(code, 0)
        self.assertEqual(result["config"]["subscription"], self.SUBSCRIPTION)
        self.assertIn(self.SUBSCRIPTION, self._mint_args())
        code, result = run(["configure", "--subscription", "default"])
        self.assertEqual(code, 0)
        self.assertIsNone(result["config"]["subscription"])
        self.assertNotIn("--subscription", self._mint_args())


class InstallHookTimeoutTypeTests(_IsolatedEnv):
    """I6: `timeoutSec` must be an integer when the value is whole."""

    def test_timeout_sec_is_integer(self):
        with tempfile.TemporaryDirectory() as repo:
            output = io.StringIO()
            with redirect_stdout(output):
                code = cli.main(["install-hook", "--scope", "repo", "--repo", repo])
            self.assertEqual(code, 0)
            result = json.loads(output.getvalue())
            payload = json.loads(Path(result["path"]).read_text(encoding="utf-8"))
            timeout = payload["hooks"]["userPromptTransformed"][0]["timeoutSec"]
            self.assertIsInstance(timeout, int)
            self.assertEqual(timeout, 2)
            self.assertTrue(result.get("notes"))


if __name__ == "__main__":
    unittest.main()
