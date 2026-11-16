"""Tests of the MCP server core: dispatch to lanes, answers, cancellation, limits."""

import json
import threading
import unittest
from collections.abc import Callable, Sequence

from agents_dispatcher.interfaces.mcp.server import McpServer, ServerLimits

from agents_dispatcher.application.lanes import Lane
from agents_dispatcher.application.tool_outcome import ToolOutcome
from agents_dispatcher.interfaces.mcp.framing import FrameError, FrameLimits
from agents_dispatcher.interfaces.mcp.outbox import Outbox
from agents_dispatcher.interfaces.mcp.protocol import INVALID_REQUEST

TOOLS: list[dict[str, object]] = [{"name": "status"}, {"name": "wait"}, {"name": "start"}]
FRAME = FrameLimits(in_bytes=4096, depth=8, nodes=1000, numeric_token_bytes=32)
LIMITS = ServerLimits(
    frame=FRAME, string_bytes=16, ids_maximum=8, aggregate_bytes=10_000, out_bytes=2_000
)


class ManualLanes:
    """Keeps jobs until the test runs them; can pretend to be full."""

    def __init__(self) -> None:
        """Start empty and open."""
        self.jobs: list[tuple[Lane, Callable[[], None]]] = []
        self.full = False

    def submit(self, lane: Lane, job: Callable[[], None]) -> bool:
        """Keep the job unless full."""
        if self.full:
            return False
        self.jobs.append((lane, job))
        return True

    def close(self) -> None:
        """Nothing to stop."""

    def run_all(self) -> None:
        """Run the kept jobs in order."""
        while self.jobs:
            self.jobs.pop(0)[1]()


class FakeEndpoint:
    """Answers scripted outcomes and records what it saw."""

    def __init__(self) -> None:
        """Start with a plain success."""
        self.outcome = ToolOutcome({"ok": True}, is_error=False)
        self.raises: Exception | None = None
        self.calls: list[tuple[str, object]] = []
        self.cancelled_seen: list[bool] = []
        self.before_return: Callable[[], None] | None = None
        self._cancelled: Callable[[], bool] = lambda: False

    def tools(self) -> Sequence[dict[str, object]]:
        """Return the fake tool list."""
        return TOOLS

    def call(self, name: str, arguments: object, cancelled: Callable[[], bool]) -> ToolOutcome:
        """Record the call; act out the script."""
        self.calls.append((name, arguments))
        self._cancelled = cancelled
        if self.before_return is not None:
            self.before_return()
        self.cancelled_seen.append(cancelled())
        if self.raises is not None:
            raise self.raises
        return self.outcome


class SpyLog:
    """Keeps the lines."""

    def __init__(self) -> None:
        """Start empty."""
        self.lines: list[tuple[str, str]] = []

    def record(self, event: str, detail: str) -> None:
        """Keep one line."""
        self.lines.append((event, detail))


class Rig:
    """A server over fakes, already initialized."""

    def __init__(self, *, outbox_bytes: int = 1_000_000) -> None:
        """Build and initialize."""
        self.endpoint = FakeEndpoint()
        self.lanes = ManualLanes()
        self.outbox = Outbox(max_bytes=outbox_bytes, priority_burst=8)
        self.log = SpyLog()
        self.shutdown = threading.Event()
        self.server = McpServer(
            endpoint=self.endpoint,
            lanes=self.lanes,
            outbox=self.outbox,
            log=self.log,
            shutdown=self.shutdown,
            limits=LIMITS,
            server_version="1.0",
            clock=lambda: 100.0,
        )
        self.send(
            {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "clientInfo": {"name": "t", "version": "1"},
                },
            }
        )
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.take()

    def send(self, message: dict[str, object]) -> None:
        """Feed one message."""
        self.server.on_frame(json.dumps(message).encode())

    def call(self, request_id: object, name: str, arguments: object = None) -> None:
        """Send a tools/call."""
        params: dict[str, object] = {"name": name}
        if arguments is not None:
            params["arguments"] = arguments
        self.send({"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": params})

    def take(self) -> list[dict[str, object]]:
        """Return what has been queued for writing, decoded."""
        frames: list[dict[str, object]] = []
        while (frame := self.outbox.get(timeout_s=0.0)) is not None:
            assert frame.endswith(b"\n") and b"\n" not in frame[:-1]  # noqa: S101 - one line
            frames.append(json.loads(frame))
        return frames


def result_of(message: dict[str, object]) -> dict[str, object]:
    """Return the result member."""
    result = message["result"]
    assert isinstance(result, dict)  # noqa: S101 - narrows the type
    return result


def error_code_of(message: dict[str, object]) -> str:
    """Return the `error.code` of a tool error payload."""
    payload = result_of(message)["structuredContent"]
    assert isinstance(payload, dict)  # noqa: S101 - narrows the type
    error = payload["error"]
    assert isinstance(error, dict)  # noqa: S101 - narrows the type
    return str(error["code"])


class DispatchTests(unittest.TestCase):
    """Calls go to lanes and come back as results."""

    def test_status_runs_on_the_control_lane_and_wait_on_the_normal_lane(self) -> None:
        """The lane follows the tool."""
        rig = Rig()
        rig.call(1, "status", {})
        rig.call(2, "wait", {})
        rig.call(3, "start", {})
        self.assertEqual(
            [lane for lane, _ in rig.lanes.jobs],
            [Lane.CONTROL, Lane.NORMAL, Lane.NORMAL],
            msg="lanes",
        )

    def test_a_result_carries_the_payload_as_data_and_as_text(self) -> None:
        """One block of text and the same object as structured content."""
        rig = Rig()
        rig.endpoint.outcome = ToolOutcome({"run_id": "r1", "state": "RUNNING"}, is_error=False)
        rig.call(7, "status", {"run_id": "r1"})
        rig.lanes.run_all()
        (message,) = rig.take()
        result = result_of(message)
        self.assertEqual(message["id"], 7, msg="answers its request")
        self.assertEqual(result["isError"], False, msg="not an error")
        self.assertEqual(result["structuredContent"], {"run_id": "r1", "state": "RUNNING"})
        content = result["content"]
        assert isinstance(content, list)  # noqa: S101 - narrows the type
        self.assertEqual(len(content), 1, msg="one block")
        self.assertEqual(content[0]["type"], "text", msg="text block")
        self.assertEqual(json.loads(content[0]["text"]), result["structuredContent"], msg="same")
        self.assertEqual(rig.endpoint.calls, [("status", {"run_id": "r1"})], msg="arguments passed")

    def test_an_error_outcome_is_a_result_with_is_error(self) -> None:
        """Tool failures are results, not protocol errors."""
        rig = Rig()
        rig.endpoint.outcome = ToolOutcome({"error": {"code": "X", "message": "m"}}, is_error=True)
        rig.call(1, "status", {})
        rig.lanes.run_all()
        (message,) = rig.take()
        self.assertTrue(result_of(message)["isError"], msg="flagged")
        self.assertNotIn("error", message, msg="not a JSON-RPC error")

    def test_an_endpoint_that_raises_becomes_an_internal_tool_error(self) -> None:
        """The server survives and the id is free again."""
        rig = Rig()
        rig.endpoint.raises = RuntimeError("secret detail")
        rig.call(1, "status", {})
        rig.lanes.run_all()
        (message,) = rig.take()
        self.assertEqual(error_code_of(message), "INTERNAL", msg="internal")
        self.assertNotIn("secret detail", json.dumps(message), msg="no internals leak")
        self.assertTrue(
            any(event == "TOOL" and "ERROR(INTERNAL)" in detail for event, detail in rig.log.lines),
            msg="logged",
        )
        rig.endpoint.raises = None
        rig.call(1, "status", {})
        rig.lanes.run_all()
        self.assertEqual(len(rig.take()), 1, msg="the id was released")

    def test_a_result_over_the_output_bound_becomes_a_small_error(self) -> None:
        """Never truncated silently."""
        rig = Rig()
        rig.endpoint.outcome = ToolOutcome({"text": "x" * 5_000}, is_error=False)
        rig.call(1, "status", {})
        rig.lanes.run_all()
        (message,) = rig.take()
        self.assertEqual(error_code_of(message), "RESULT_TOO_LARGE", msg="named")
        self.assertLessEqual(len(json.dumps(message)), LIMITS.out_bytes, msg="fits")

    def test_a_control_result_overtakes_a_queued_normal_result(self) -> None:
        """Priority in the outbox."""
        rig = Rig()
        rig.call(1, "wait", {})
        rig.call(2, "status", {})
        rig.lanes.jobs.reverse()
        rig.lanes.run_all()
        ids = [message["id"] for message in rig.take()]
        self.assertEqual(ids, [2, 1], msg="status answered first")


class AdmissionTests(unittest.TestCase):
    """What is refused before it runs."""

    def test_a_full_lane_answers_busy_at_once_and_frees_the_id(self) -> None:
        """A retriable tool error, not a protocol error."""
        rig = Rig()
        rig.lanes.full = True
        rig.call(1, "wait", {})
        (message,) = rig.take()
        self.assertEqual(error_code_of(message), "BUSY", msg="busy")
        rig.lanes.full = False
        rig.call(1, "wait", {})
        self.assertEqual(len(rig.lanes.jobs), 1, msg="the id can be used again")

    def test_a_duplicate_in_flight_id_is_an_invalid_request(self) -> None:
        """JSON-RPC level refusal."""
        rig = Rig()
        rig.call(1, "wait", {})
        rig.call(1, "status", {})
        (message,) = rig.take()
        error = message["error"]
        assert isinstance(error, dict)  # noqa: S101 - narrows the type
        self.assertEqual(error["code"], INVALID_REQUEST, msg="duplicate refused")
        self.assertEqual(len(rig.lanes.jobs), 1, msg="only the first was queued")

    def test_requests_holding_too_many_bytes_are_busy_until_one_finishes(self) -> None:
        """The aggregate input bound."""
        rig = Rig()
        big = "x" * 4_000
        rig.call(1, "wait", {"pad": big})
        rig.call(2, "wait", {"pad": big})
        rig.call(3, "wait", {"pad": big})
        (message,) = rig.take()
        self.assertEqual((message["id"], error_code_of(message)), (3, "BUSY"), msg="third refused")
        rig.lanes.run_all()
        rig.take()
        rig.call(4, "wait", {"pad": big})
        self.assertEqual(len(rig.lanes.jobs), 1, msg="room after the first two finished")

    def test_a_refused_frame_is_answered_with_a_null_id(self) -> None:
        """Oversize or stalled frames reported by the splitter."""
        rig = Rig()
        rig.server.on_refused(FrameError(INVALID_REQUEST, "too long"))
        (message,) = rig.take()
        self.assertIsNone(message["id"], msg="null id")

    def test_malformed_json_is_a_parse_error_and_the_server_goes_on(self) -> None:
        """-32700 then normal service."""
        rig = Rig()
        rig.server.on_frame(b"{not json")
        (message,) = rig.take()
        error = message["error"]
        assert isinstance(error, dict)  # noqa: S101 - narrows the type
        self.assertEqual(error["code"], -32700, msg="parse error")
        rig.send({"jsonrpc": "2.0", "id": 5, "method": "ping"})
        self.assertEqual(len(rig.take()), 1, msg="still serving")


class CancellationTests(unittest.TestCase):
    """notifications/cancelled and shutdown."""

    def test_a_cancelled_request_gets_no_response_and_the_tool_sees_the_flag(self) -> None:
        """The wait returns early; nothing is written."""
        rig = Rig()
        rig.call(1, "wait", {})
        rig.endpoint.before_return = lambda: rig.send(
            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}}
        )
        rig.lanes.run_all()
        self.assertEqual(rig.take(), [], msg="no response")
        self.assertEqual(rig.endpoint.cancelled_seen, [True], msg="flag visible to the tool")
        self.assertIn(("TOOL", "tool=wait lane=normal outcome=CANCELLED ms=0"), rig.log.lines)
        rig.endpoint.before_return = None
        rig.call(1, "wait", {})
        self.assertEqual(len(rig.lanes.jobs), 1, msg="the id was released")

    def test_a_cancelled_start_that_committed_a_run_is_logged_with_the_run_id(self) -> None:
        """The run is kept; the log says where it is."""
        rig = Rig()
        rig.endpoint.outcome = ToolOutcome({"run_id": "run-9", "state": "RUNNING"}, is_error=False)
        rig.call(1, "start", {})
        rig.endpoint.before_return = lambda: rig.send(
            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}}
        )
        rig.lanes.run_all()
        self.assertEqual(rig.take(), [], msg="no response")
        self.assertIn(("CANCELLED_AFTER_COMMIT", "tool=start run=run-9"), rig.log.lines)

    def test_cancelling_an_unknown_request_changes_nothing(self) -> None:
        """Ignored."""
        rig = Rig()
        rig.send(
            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 99}}
        )
        self.assertEqual(rig.take(), [], msg="nothing")

    def test_the_shutdown_flag_reads_as_cancelled_to_the_tool(self) -> None:
        """A wait ends when the server is going away."""
        rig = Rig()
        rig.call(1, "wait", {})
        rig.shutdown.set()
        rig.lanes.run_all()
        self.assertEqual(rig.endpoint.cancelled_seen, [True], msg="shutdown seen as cancel")


class OutputFailureTests(unittest.TestCase):
    """A client that stops reading."""

    def test_an_outbox_that_cannot_take_an_answer_stops_the_server(self) -> None:
        """Close admission instead of dropping answers silently."""
        rig = Rig(outbox_bytes=300)
        for index in range(3):
            rig.send({"jsonrpc": "2.0", "id": index + 1, "method": "ping"})
        self.assertFalse(rig.shutdown.is_set(), msg="room for a few answers")
        for index in range(20):
            rig.send({"jsonrpc": "2.0", "id": index + 10, "method": "ping"})
        self.assertTrue(rig.shutdown.is_set(), msg="shutdown requested")
        self.assertTrue(rig.server.output_failed(), msg="and the reason is output failure")


if __name__ == "__main__":
    unittest.main()
