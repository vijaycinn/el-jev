from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from queue import Queue
from threading import Thread


SERVER = Path(__file__).with_name("server.py")


class McpServerProcess:
    def __init__(self) -> None:
        environment = os.environ.copy()
        environment["ELJEV_PORT"] = "1"
        self.process = subprocess.Popen(
            [sys.executable, str(SERVER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        self.output_queue: Queue[str | BaseException] = Queue()
        self.reader = Thread(target=self._read_output, daemon=True)
        self.reader.start()

    def _read_output(self) -> None:
        assert self.process.stdout is not None
        try:
            for line in self.process.stdout:
                self.output_queue.put(line)
        except BaseException as error:
            self.output_queue.put(error)

    def request(self, message: dict[str, object]) -> dict[str, object]:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()
        value = self.output_queue.get(timeout=5)
        if isinstance(value, BaseException):
            raise value
        return json.loads(value)

    def notify(self, message: dict[str, object]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def close(self) -> None:
        if self.process.poll() is None:
            assert self.process.stdin is not None
            self.process.stdin.close()
            self.process.terminate()
            self.process.wait(timeout=5)
        if self.process.stdin is not None and not self.process.stdin.closed:
            self.process.stdin.close()
        if self.process.stdout is not None:
            self.process.stdout.close()
        if self.process.stderr is not None:
            self.process.stderr.close()


class McpServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server = McpServerProcess()

    def tearDown(self) -> None:
        self.server.close()

    def test_initialize_list_and_health_when_daemon_is_down(self) -> None:
        initialized = self.server.request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            }
        )
        self.assertIn("result", initialized)
        self.assertEqual(initialized["result"]["protocolVersion"], "2025-06-18")

        tools = self.server.request({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tool_names = {tool["name"] for tool in tools["result"]["tools"]}
        self.assertEqual(tool_names, {"eljev_screen", "eljev_decide", "eljev_health"})

        health = self.server.request(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "eljev_health", "arguments": {}},
            }
        )
        self.assertNotIn("error", health)
        structured = health["result"]["structuredContent"]
        self.assertEqual(structured["error_kind"], "daemon_unavailable")
        self.assertIn("eljev daemon start", structured["instructions"])
        self.assertEqual(structured["coverage_policy"], "always_abstain_v0")
        self.assertEqual(structured["calibration_version"], "none")
        self.assertTrue(health["result"]["isError"])

        shutdown = self.server.request({"jsonrpc": "2.0", "id": 4, "method": "shutdown"})
        self.assertIsNone(shutdown["result"])
        self.server.notify({"jsonrpc": "2.0", "method": "exit"})
        self.server.process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
