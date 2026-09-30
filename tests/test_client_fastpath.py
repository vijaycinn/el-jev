import json
import socket
import subprocess
import sys
import threading
import time
import unittest

from eljev.client import EljevClient


ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


def _closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class ClientFastPathTests(unittest.TestCase):
    def test_forbidden_modules_are_absent_when_importing_client(self):
        script = (
            "import eljev.client, sys; "
            "assert 'http.client' not in sys.modules, sorted(sys.modules); "
            "assert 'email' not in sys.modules, sorted(sys.modules); "
            "assert 'argparse' not in sys.modules, sorted(sys.modules); "
            "assert 'urllib.request' not in sys.modules, sorted(sys.modules)"
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
            "print(sorted(m for m in sys.modules if m in "
            "('http.client','email','argparse','urllib.request')))"
        )
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual(completed.stdout.strip(), "[]")

    def test_shutdown_post_sends_required_headers(self):
        captured: list[str] = []
        ready = threading.Event()

        class _Server:
            def __init__(self):
                self.socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                self.socket.bind(("127.0.0.1", 0))
                self.socket.listen(1)
                self.port = self.socket.getsockname()[1]
                self.thread = threading.Thread(target=self._serve, daemon=True)

            def _serve(self):
                ready.set()
                connection, _ = self.socket.accept()
                with connection:
                    data = bytearray()
                    while b"\r\n\r\n" not in data:
                        chunk = connection.recv(4096)
                        if not chunk:
                            return
                        data.extend(chunk)
                    captured.append(data.decode("utf-8", "replace"))
                    body = b'{"status":"stopping","pid":1}'
                    connection.sendall(
                        b"HTTP/1.1 200 OK\r\n"
                        b"Content-Type: application/json\r\n"
                        + f"Content-Length: {len(body)}\r\n".encode("ascii")
                        + b"Connection: close\r\n\r\n"
                        + body
                    )

            def start(self):
                self.thread.start()

            def close(self):
                self.socket.close()
                self.thread.join(timeout=2)

        server = _Server()
        server.start()
        ready.wait(timeout=2)
        try:
            result = EljevClient(port=server.port).shutdown()
        finally:
            server.close()

        self.assertEqual(result, {"status": "stopping", "pid": 1})
        self.assertTrue(captured)
        request = captured[0].replace("\r", "")
        request_lower = request.lower()
        self.assertIn("POST /v1/shutdown HTTP/1.1", request)
        self.assertIn(f"Host: 127.0.0.1:{server.port}", request)
        self.assertIn("Content-Type: application/json", request)
        self.assertNotIn("\norigin:", request_lower)

    def test_shutdown_returns_down_when_port_is_closed(self):
        port = _closed_port()
        result = EljevClient(port=port).shutdown()
        self.assertEqual(result.get("status"), "down")

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
