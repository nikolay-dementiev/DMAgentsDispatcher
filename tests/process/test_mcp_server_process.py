"""The MCP server as a real process: framing on real pipes, shutdown, lanes and cancellation."""

import json
import os
import queue
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Mapping
from pathlib import Path

from tests.support.run_spies import RUN_ID

ROOT = Path(__file__).resolve().parents[2]
RUN = RUN_ID.value
WAIT_S = 20.0
INIT = {
    "jsonrpc": "2.0",
    "id": "init",
    "method": "initialize",
    "params": {"protocolVersion": "2025-11-25", "clientInfo": {"name": "t", "version": "1"}},
}


def call(request_id: object, name: str, arguments: dict[str, object]) -> dict[str, object]:
    """Build a `tools/call` request."""
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }


class Server:
    """A server process with a reader thread that collects the lines of standard output."""

    def __init__(self, directory: str, settings: str = "") -> None:
        """Start the child; `settings` is user TOML over the shipped defaults."""
        env = {
            **os.environ,
            "MCP_TEST_LOG_DIR": directory,
            "MCP_TEST_SETTINGS": settings,
        }
        self.process = subprocess.Popen(
            [sys.executable, "-m", "tests.support.mcp_child"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=ROOT,
            env=env,
        )
        self.lines: queue.Queue[bytes] = queue.Queue()
        self.raw: list[bytes] = []
        self.errors: list[bytes] = []
        self._readers = [
            threading.Thread(target=self._read, daemon=True),
            threading.Thread(target=self._read_errors, daemon=True),
        ]
        for reader in self._readers:
            reader.start()

    def _read(self) -> None:
        """Move each standard output line to the queue."""
        assert self.process.stdout is not None  # noqa: S101 - narrows the type
        for line in self.process.stdout:
            self.raw.append(line)
            self.lines.put(line)

    def _read_errors(self) -> None:
        """Collect standard error so the child never blocks on a full pipe."""
        assert self.process.stderr is not None  # noqa: S101 - narrows the type
        for line in self.process.stderr:
            self.errors.append(line)

    def send(self, message: Mapping[str, object]) -> None:
        """Write one frame."""
        assert self.process.stdin is not None  # noqa: S101 - narrows the type
        self.process.stdin.write(json.dumps(message).encode() + b"\n")
        self.process.stdin.flush()

    def receive(self, timeout_s: float = WAIT_S) -> dict[str, object]:
        """Return the next frame; every line must be a JSON-RPC message."""
        parsed = json.loads(self.lines.get(timeout=timeout_s))
        assert isinstance(parsed, dict)  # noqa: S101 - narrows the type
        return parsed

    def open(self) -> None:
        """Initialize the session."""
        self.send(INIT)
        self.receive()
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def close_input(self) -> None:
        """End the client's side of the session."""
        assert self.process.stdin is not None  # noqa: S101 - narrows the type
        self.process.stdin.close()

    def finish(self, timeout_s: float = WAIT_S) -> int:
        """Wait for the exit and return its code."""
        try:
            return self.process.wait(timeout=timeout_s)
        finally:
            self.close()

    def close(self) -> None:
        """Kill a child that is still running and close every pipe."""
        self.process.kill()
        self.process.wait()
        for reader in self._readers:
            reader.join(5)
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None and not stream.closed:
                stream.close()


class McpProcessTests(unittest.TestCase):
    """One server process per test."""

    def setUp(self) -> None:
        """Make a log folder."""
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.logs = self._tmp.name

    def start(self, settings: str = "") -> Server:
        """Start a server and make sure it does not outlive the test."""
        server = Server(self.logs, settings)
        self.addCleanup(server.close)
        return server

    def log_text(self) -> str:
        """Return the SDK log, empty when nothing was written."""
        path = Path(self.logs) / "sdk.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def test_only_frames_reach_standard_output_even_when_a_use_case_prints(self) -> None:
        """A print and a raw write to descriptor 1 inside a tool must not corrupt the stream."""
        server = self.start()
        server.open()
        server.send(call(1, "status", {"run_id": RUN}))
        answer = server.receive()
        self.assertEqual(answer["id"], 1, msg="answered the call")
        server.close_input()
        self.assertEqual(server.finish(), 0, msg="clean exit")
        for line in server.raw:
            self.assertIsInstance(json.loads(line), dict, msg=f"{line!r} is a frame")
        self.assertEqual(len(server.raw), 2, msg="initialize answer and tool answer only")

    def test_end_of_input_ends_the_server_within_the_shutdown_bound(self) -> None:
        """Closing the pipe is the normal end; the log says why."""
        server = self.start()
        server.open()
        started = time.monotonic()
        server.close_input()
        self.assertEqual(server.finish(), 0, msg="exit code 0")
        self.assertLess(time.monotonic() - started, 5.0 + 2.0, msg="within shutdown_total_s")
        self.assertIn("MCP SERVE_END reason=EOF", self.log_text(), msg="logged")

    def test_status_and_stop_answer_while_a_wait_is_blocked(self) -> None:
        """The control lane is never queued behind the normal lane."""
        server = self.start("[mcp.normal]\nworkers = 1\nqueue = 4\n")
        server.open()
        server.send(call("w", "wait", {"run_id": RUN}))
        server.send(call("s", "status", {"run_id": RUN}))
        server.send(call("x", "stop", {"run_id": RUN, "reason": "r"}))
        first, second = server.receive(), server.receive()
        self.assertEqual({first["id"], second["id"]}, {"s", "x"}, msg="control answers came first")
        server.close_input()
        self.assertEqual(server.finish(), 0, msg="the blocked wait did not hold the exit")

    def test_end_of_input_cancels_a_blocked_wait_so_the_drain_is_not_used_up(self) -> None:
        """The wait is told to stop, returns, and the exit does not wait out the drain bound."""
        server = self.start()
        server.open()
        server.send(call("w", "wait", {"run_id": RUN}))
        time.sleep(0.3)
        started = time.monotonic()
        server.close_input()
        self.assertEqual(server.finish(), 0, msg="exit code 0")
        self.assertLess(time.monotonic() - started, 1.5, msg="well inside shutdown_drain_s = 2")

    def test_a_cancelled_wait_gets_no_answer(self) -> None:
        """`notifications/cancelled` ends the wait quietly."""
        server = self.start()
        server.open()
        server.send(call("w", "wait", {"run_id": RUN}))
        time.sleep(0.3)
        server.send(
            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": "w"}}
        )
        server.send({"jsonrpc": "2.0", "id": "p", "method": "ping"})
        self.assertEqual(server.receive()["id"], "p", msg="the next frame is the ping answer")
        server.close_input()
        self.assertEqual(server.finish(), 0, msg="exit")
        ids = [json.loads(line).get("id") for line in server.raw]
        self.assertNotIn("w", ids, msg="no answer for the cancelled call")

    def test_a_full_normal_lane_answers_busy(self) -> None:
        """One worker and one queue place hold two waits; the third call is refused."""
        server = self.start("[mcp.normal]\nworkers = 1\nqueue = 1\n")
        server.open()
        for name in ("a", "b", "c"):
            server.send(call(name, "wait", {"run_id": RUN}))
            time.sleep(0.3)  # let the worker take the first call before the next arrives
        answer = server.receive()
        self.assertEqual(answer["id"], "c", msg="the third call was refused at once")
        result = answer["result"]
        assert isinstance(result, dict)  # noqa: S101 - narrows the type
        self.assertTrue(result["isError"], msg="a tool error")
        self.assertEqual(result["structuredContent"]["error"]["code"], "BUSY", msg="BUSY")
        server.close_input()
        self.assertEqual(server.finish(), 0, msg="exit")

    def test_two_submissions_run_at_the_same_time_in_one_process(self) -> None:
        """The start use cases meet at a barrier, which only opens when both are inside."""
        server = self.start()
        server.open()
        for name in ("r1", "r2"):
            server.send(call(name, "route", {"repo": "/r", "task": "t"}))
        answers = {server.receive()["id"], server.receive()["id"]}
        self.assertEqual(answers, {"r1", "r2"}, msg="both submissions answered")
        server.close_input()
        self.assertEqual(server.finish(), 0, msg="exit")

    def test_sigterm_ends_the_server_even_with_a_wait_in_flight(self) -> None:
        """A signal is a clean end: the blocked wait is told to stop."""
        server = self.start()
        server.open()
        server.send(call("w", "wait", {"run_id": RUN}))
        time.sleep(0.3)
        started = time.monotonic()
        server.process.send_signal(signal.SIGTERM)
        self.assertEqual(server.finish(), 0, msg="exit code 0")
        self.assertLess(time.monotonic() - started, 5.0 + 2.0, msg="within shutdown_total_s")
        self.assertIn("MCP SERVE_END reason=SIGNAL", self.log_text(), msg="logged")

    def test_a_stalled_start_is_ended_by_the_initialize_timeout(self) -> None:
        """A client that never speaks does not hold the process forever."""
        server = self.start('[mcp]\ninitialize_timeout_s = "0.5"\n')
        self.assertEqual(server.finish(), 1, msg="exit code 1")
        self.assertIn("reason=INIT_TIMEOUT", self.log_text(), msg="logged")


if __name__ == "__main__":
    unittest.main()
