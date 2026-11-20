"""Tests of the stream transport: the loop that joins bytes in, the server core and bytes out."""

import json
import queue
import threading
import time
import unittest
from collections.abc import Callable, Sequence

from agents_dispatcher.adapters.system.mcp_lanes import ThreadLanes
from agents_dispatcher.application.lanes import Lane
from agents_dispatcher.application.tool_outcome import ToolOutcome
from agents_dispatcher.interfaces.mcp.framing import FrameLimits
from agents_dispatcher.interfaces.mcp.server import ServerLimits
from agents_dispatcher.interfaces.mcp.stream_transport import StreamTransport, Timings

WAIT_S = 10.0
NORMAL_WORKERS = 2
TOOLS: list[dict[str, object]] = [{"name": "status"}, {"name": "wait"}]
LIMITS = ServerLimits(
    frame=FrameLimits(in_bytes=2048, depth=8, nodes=1000, numeric_token_bytes=32),
    string_bytes=16,
    ids_maximum=16,
    aggregate_bytes=100_000,
    out_bytes=10_000,
)
TIMINGS = Timings(
    initialize_timeout_s=5.0,
    partial_frame_timeout_s=0.3,
    write_timeout_s=1.0,
    shutdown_drain_s=0.3,
    shutdown_total_s=1.0,
    poll_s=0.01,
)
INIT = {
    "jsonrpc": "2.0",
    "id": 0,
    "method": "initialize",
    "params": {"protocolVersion": "2025-11-25", "clientInfo": {"name": "t", "version": "1"}},
}


def line(message: dict[str, object]) -> bytes:
    """Encode a message as one frame."""
    return json.dumps(message).encode() + b"\n"


class QueueInput:
    """Bytes the test feeds; b"" is end of input."""

    def __init__(self) -> None:
        """Start empty."""
        self.chunks: queue.Queue[bytes] = queue.Queue()

    def feed(self, data: bytes) -> None:
        """Make bytes readable."""
        self.chunks.put(data)

    def eof(self) -> None:
        """End the input."""
        self.chunks.put(b"")

    def read(self, timeout_s: float) -> bytes | None:
        """Return bytes, b"" at the end, or None when nothing came in time."""
        try:
            return self.chunks.get(timeout=timeout_s)
        except queue.Empty:
            return None


class ListOutput:
    """Collects what is written; can start failing."""

    def __init__(self) -> None:
        """Start working."""
        self.frames: list[dict[str, object]] = []
        self.fail = False
        self._arrived = threading.Condition()

    def write(self, data: bytes, timeout_s: float) -> bool:  # noqa: ARG002 - the port's signature
        """Keep a frame, or refuse when failing."""
        if self.fail:
            return False
        with self._arrived:
            self.frames.append(json.loads(data))
            self._arrived.notify_all()
        return True

    def wait_for(self, count: int) -> bool:
        """Wait until `count` frames arrived."""
        with self._arrived:
            return self._arrived.wait_for(lambda: len(self.frames) >= count, WAIT_S)


class ScriptedEndpoint:
    """A `wait` that blocks until released or cancelled, and a quick `status`."""

    def __init__(self) -> None:
        """Start unreleased."""
        self.release = threading.Event()
        self.stuck_forever = threading.Event()
        self.ignore_cancel = False
        self.started = threading.Event()
        self.started_count = 0
        self.status_delay_s = 0.0
        self.status_started = threading.Event()

    def tools(self) -> Sequence[dict[str, object]]:
        """Return the tools."""
        return TOOLS

    def call(self, name: str, arguments: object, cancelled: Callable[[], bool]) -> ToolOutcome:  # noqa: ARG002
        """`wait` blocks; `status` answers."""
        if name == "status":
            self.status_started.set()
            time.sleep(self.status_delay_s)
        if name == "wait":
            self.started_count += 1
            self.started.set()
            if self.ignore_cancel:
                self.stuck_forever.wait(WAIT_S)
            while not self.release.is_set() and not cancelled():
                time.sleep(0.005)
        return ToolOutcome({"tool": name}, is_error=False)


class SpyLog:
    """Keeps the lines."""

    def __init__(self) -> None:
        """Start empty."""
        self.lines: list[tuple[str, str]] = []

    def record(self, event: str, detail: str) -> None:
        """Keep one line."""
        self.lines.append((event, detail))

    def events(self) -> list[str]:
        """Return the event names."""
        return [event for event, _ in self.lines]


class Rig:
    """A transport over in-memory input and output, served on its own thread."""

    def __init__(self, timings: Timings = TIMINGS) -> None:
        """Build; `start` runs it."""
        self.input = QueueInput()
        self.output = ListOutput()
        self.endpoint = ScriptedEndpoint()
        self.log = SpyLog()
        self.shutdown = threading.Event()
        self.lanes = ThreadLanes(
            control_workers=1,
            control_queue=4,
            normal_workers=NORMAL_WORKERS,
            normal_queue=4,
            on_job_error=lambda error: self.log.record("JOB_ERROR", repr(error)),
        )
        self.transport = StreamTransport(
            input=self.input,
            output=self.output,
            lanes=self.lanes,
            limits=LIMITS,
            timings=timings,
            log=self.log,
            server_version="1.0",
            outbox_bytes=1_000_000,
            priority_burst=8,
            clock=time.monotonic,
        )
        self.code: list[int] = []
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def _serve(self) -> None:
        """Run the transport and keep its exit code."""
        self.code.append(self.transport.serve(self.endpoint, self.shutdown))

    def start(self) -> None:
        """Start serving."""
        self.thread.start()

    def finish(self) -> int:
        """Wait for serve to return and give its exit code."""
        self.thread.join(WAIT_S)
        self.assertion_alive()
        return self.code[0]

    def assertion_alive(self) -> None:
        """Fail loudly when serve did not return."""
        if self.thread.is_alive():
            message = "serve did not return"
            raise AssertionError(message)


class StreamTransportTests(unittest.TestCase):
    """The loop end to end, with real threads and in-memory bytes."""

    def test_initialize_ping_and_end_of_input_exit_zero(self) -> None:
        """The simplest session, and its log."""
        rig = Rig()
        rig.start()
        rig.input.feed(line(INIT) + line({"jsonrpc": "2.0", "id": 1, "method": "ping"}))
        rig.input.eof()
        self.assertEqual(rig.finish(), 0, msg="exit 0 on end of input")
        self.assertEqual([frame["id"] for frame in rig.output.frames], [0, 1], msg="both answered")
        self.assertEqual(
            rig.log.events(), ["SERVE_START", "INITIALIZED", "SERVE_END"], msg="log lines"
        )
        self.assertIn(("SERVE_END", "reason=EOF log_failures=0"), rig.log.lines, msg="reason")

    def test_status_is_answered_while_a_wait_is_blocked(self) -> None:
        """The control lane is not behind the normal lane."""
        rig = Rig()
        rig.start()
        rig.input.feed(line(INIT))
        rig.input.feed(
            line(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "wait", "arguments": {}},
                }
            )
        )
        self.assertTrue(rig.endpoint.started.wait(WAIT_S), msg="the wait is running")
        rig.input.feed(
            line(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {"name": "status", "arguments": {}},
                }
            )
        )
        self.assertTrue(rig.output.wait_for(2), msg="initialize and status answered")
        self.assertEqual([f["id"] for f in rig.output.frames], [0, 2], msg="status first")
        rig.endpoint.release.set()
        self.assertTrue(rig.output.wait_for(3), msg="then the wait")
        rig.input.eof()
        self.assertEqual(rig.finish(), 0, msg="clean exit")

    def test_a_cancelled_wait_is_never_answered(self) -> None:
        """notifications/cancelled ends the tool without a response."""
        rig = Rig()
        rig.start()
        rig.input.feed(line(INIT))
        rig.input.feed(
            line(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "wait", "arguments": {}},
                }
            )
        )
        self.assertTrue(rig.endpoint.started.wait(WAIT_S), msg="running")
        rig.input.feed(
            line(
                {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}}
            )
        )
        rig.input.feed(line({"jsonrpc": "2.0", "id": 3, "method": "ping"}))
        self.assertTrue(rig.output.wait_for(2), msg="the ping after it is answered")
        time.sleep(0.2)
        rig.input.eof()
        self.assertEqual(rig.finish(), 0, msg="clean exit")
        self.assertEqual([f["id"] for f in rig.output.frames], [0, 3], msg="no answer for id 1")
        self.assertTrue(any("CANCELLED" in detail for _, detail in rig.log.lines), msg="logged")

    def test_a_stalled_partial_frame_is_answered_with_an_error_and_service_goes_on(self) -> None:
        """The partial frame timeout."""
        rig = Rig()
        rig.start()
        rig.input.feed(line(INIT))
        rig.input.feed(b'{"jsonrpc":"2.0","id":5,"met')
        self.assertTrue(rig.output.wait_for(2), msg="the error arrives after the timeout")
        self.assertIsNone(rig.output.frames[1]["id"], msg="null id")
        rig.input.feed(b'hod":"x"}\n' + line({"jsonrpc": "2.0", "id": 6, "method": "ping"}))
        self.assertTrue(rig.output.wait_for(3), msg="served again")
        self.assertEqual(rig.output.frames[2]["id"], 6, msg="the next frame was served")
        rig.input.eof()
        rig.finish()

    def test_an_oversize_line_is_refused_and_the_next_one_is_served(self) -> None:
        """Bounded reading."""
        rig = Rig()
        rig.start()
        rig.input.feed(line(INIT))
        rig.input.feed(b"x" * 5000 + b"\n" + line({"jsonrpc": "2.0", "id": 7, "method": "ping"}))
        self.assertTrue(rig.output.wait_for(3), msg="refusal and ping")
        self.assertIsNone(rig.output.frames[1]["id"], msg="refusal has a null id")
        self.assertEqual(rig.output.frames[2]["id"], 7, msg="ping served")
        rig.input.eof()
        rig.finish()

    def test_no_initialize_in_time_exits_one(self) -> None:
        """A connection that never speaks."""
        rig = Rig(
            Timings(
                initialize_timeout_s=0.1,
                partial_frame_timeout_s=0.3,
                write_timeout_s=1.0,
                shutdown_drain_s=0.1,
                shutdown_total_s=0.5,
                poll_s=0.01,
            )
        )
        rig.start()
        self.assertEqual(rig.finish(), 1, msg="exit 1")
        self.assertIn(
            ("SERVE_END", "reason=INIT_TIMEOUT log_failures=0"), rig.log.lines, msg="reason"
        )

    def test_a_failing_output_exits_one(self) -> None:
        """The client stopped reading."""
        rig = Rig()
        rig.output.fail = True
        rig.start()
        rig.input.feed(line(INIT))
        self.assertEqual(rig.finish(), 1, msg="exit 1")
        self.assertIn(
            ("SERVE_END", "reason=OUTPUT_FAILURE log_failures=0"), rig.log.lines, msg="reason"
        )

    def test_a_shutdown_request_ends_the_server_and_the_wait_with_exit_zero(self) -> None:
        """A signal sets the event; the blocked tool sees it as a cancel."""
        rig = Rig()
        rig.start()
        rig.input.feed(line(INIT))
        rig.input.feed(
            line(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "wait", "arguments": {}},
                }
            )
        )
        self.assertTrue(rig.endpoint.started.wait(WAIT_S), msg="running")
        rig.shutdown.set()
        self.assertEqual(rig.finish(), 0, msg="exit 0")
        self.assertIn(("SERVE_END", "reason=SIGNAL log_failures=0"), rig.log.lines, msg="reason")

    def test_a_tool_that_ignores_cancellation_cannot_hold_the_exit_past_the_total_bound(
        self,
    ) -> None:
        """Daemon threads are abandoned after `shutdown_total_s`."""
        rig = Rig()
        rig.endpoint.ignore_cancel = True
        rig.start()
        rig.input.feed(line(INIT))
        rig.input.feed(
            line(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "wait", "arguments": {}},
                }
            )
        )
        self.assertTrue(rig.endpoint.started.wait(WAIT_S), msg="running")
        started = time.monotonic()
        rig.input.eof()
        self.assertEqual(rig.finish(), 0, msg="exit 0")
        self.assertLess(time.monotonic() - started, TIMINGS.shutdown_total_s + 1.0, msg="bounded")
        rig.endpoint.stuck_forever.set()

    def test_end_of_input_cancels_a_blocked_wait_and_closes_the_lanes(self) -> None:
        """Nothing is left running that the client could still be waiting for."""
        rig = Rig()
        rig.start()
        rig.input.feed(line(INIT))
        rig.input.feed(
            line(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "wait", "arguments": {}},
                }
            )
        )
        self.assertTrue(rig.endpoint.started.wait(WAIT_S), msg="running")
        rig.input.eof()
        self.assertEqual(rig.finish(), 0, msg="clean exit")
        deadline = time.monotonic() + WAIT_S
        while time.monotonic() < deadline and not any(
            event == "TOOL" and "tool=wait" in detail for event, detail in rig.log.lines
        ):
            time.sleep(0.01)
        self.assertTrue(
            any(event == "TOOL" and "tool=wait" in detail for event, detail in rig.log.lines),
            msg="the wait saw the shutdown as a cancel and ended",
        )
        self.assertFalse(
            rig.lanes.submit(Lane.CONTROL, lambda: None), msg="the lanes accept nothing afterwards"
        )


def call_line(request_id: int, name: str) -> bytes:
    """Encode a `tools/call` with no arguments."""
    return line(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": {}},
        }
    )


class ShutdownAccountingTests(unittest.TestCase):
    """What a client is owed when the input ends or a signal arrives."""

    def test_a_call_that_finishes_within_the_drain_is_still_answered(self) -> None:
        """The answer of a call in flight at end of input is written before the output closes."""
        rig = Rig()
        rig.endpoint.status_delay_s = TIMINGS.shutdown_drain_s / 3
        rig.start()
        rig.input.feed(line(INIT) + call_line(1, "status"))
        self.assertTrue(rig.endpoint.status_started.wait(WAIT_S), msg="the call is running")
        rig.input.eof()
        self.assertEqual(rig.finish(), 0, msg="clean exit")
        answered = [frame.get("id") for frame in rig.output.frames]
        self.assertIn(1, answered, msg="the call in flight got its answer")

    def test_calls_still_queued_at_shutdown_are_answered_shutting_down(self) -> None:
        """Both normal workers are stuck; the third call never starts and is told so."""
        rig = Rig()
        rig.endpoint.ignore_cancel = True
        rig.start()
        rig.input.feed(line(INIT) + call_line(1, "wait") + call_line(2, "wait"))
        deadline = time.monotonic() + WAIT_S
        while rig.endpoint.started_count < NORMAL_WORKERS and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertEqual(
            rig.endpoint.started_count, NORMAL_WORKERS, msg="both normal workers are busy"
        )
        rig.input.feed(call_line(3, "wait"))
        time.sleep(0.1)
        rig.input.eof()
        self.assertEqual(rig.finish(), 0, msg="clean exit")
        rig.endpoint.stuck_forever.set()
        answers = {frame.get("id"): frame for frame in rig.output.frames}
        self.assertIn(3, answers, msg="the queued call is answered")
        result = answers[3]["result"]
        assert isinstance(result, dict)  # noqa: S101 - narrows the type
        structured = result["structuredContent"]
        assert isinstance(structured, dict)  # noqa: S101 - narrows the type
        error = structured["error"]
        assert isinstance(error, dict)  # noqa: S101 - narrows the type
        self.assertEqual(error["code"], "SHUTTING_DOWN", msg="and told why")


class ReviewFindingsTests(unittest.TestCase):
    """Failures inside the loop still shut the server down in order."""

    def test_a_read_error_is_logged_and_the_server_still_shuts_down(self) -> None:
        """An exception from the input ends the loop with a reason, not with a lost shutdown."""
        rig = Rig()

        def broken(_timeout_s: float) -> bytes | None:
            raise OSError(5, "input/output error")

        rig.input.read = broken  # type: ignore[method-assign,assignment]
        rig.start()
        self.assertEqual(rig.finish(), 1, msg="exit 1")
        self.assertIn(
            ("SERVE_END", "reason=READ_ERROR log_failures=0"), rig.log.lines, msg="reason"
        )

    def test_a_writer_that_raises_ends_the_server_as_an_output_failure(self) -> None:
        """A writer thread that dies silently would leave every call unanswered."""
        rig = Rig()

        def broken(_data: bytes, _timeout_s: float) -> bool:
            message = "file descriptor out of range"
            raise ValueError(message)

        rig.output.write = broken  # type: ignore[method-assign,assignment]
        rig.start()
        rig.input.feed(line(INIT))
        self.assertEqual(rig.finish(), 1, msg="exit 1")
        self.assertIn(
            ("SERVE_END", "reason=OUTPUT_FAILURE log_failures=0"), rig.log.lines, msg="reason"
        )


if __name__ == "__main__":
    unittest.main()
