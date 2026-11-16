"""The MCP server core: one frame in, actions out, tool calls on lanes, answers to the outbox.

It owns no file descriptor and no thread. The transport feeds it frames from one reactor thread;
the lanes run the tool calls; the outbox hands the answers to the writer.
"""

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass

from agents_dispatcher.application.lanes import Lane, LaneRunner
from agents_dispatcher.application.tool_outcome import ToolOutcome, error_outcome
from agents_dispatcher.interfaces.mcp.framing import (
    FrameError,
    FrameLimits,
    encode_frame,
    parse_frame,
)
from agents_dispatcher.interfaces.mcp.outbox import Outbox
from agents_dispatcher.interfaces.mcp.ports import McpEventLog, ToolEndpoint
from agents_dispatcher.interfaces.mcp.protocol import (
    INVALID_REQUEST,
    Admission,
    InFlightIds,
    MessageId,
    classify,
    error_message,
    result_message,
)
from agents_dispatcher.interfaces.mcp.session import (
    Action,
    CallTool,
    Cancel,
    Log,
    Reply,
    Session,
)

CONTROL_TOOLS = frozenset({"status", "stop"})
_COMMITTING_TOOLS = frozenset({"route", "start"})
_MS = 1000


@dataclass(frozen=True)
class ServerLimits:
    """The bounds the server enforces."""

    frame: FrameLimits
    string_bytes: int
    ids_maximum: int
    aggregate_bytes: int
    out_bytes: int


class McpServer:
    """Serves one connection."""

    def __init__(  # noqa: PLR0913 - the collaborators of a server, injected by its composition
        self,
        *,
        endpoint: ToolEndpoint,
        lanes: LaneRunner,
        outbox: Outbox,
        log: McpEventLog,
        shutdown: threading.Event,
        limits: ServerLimits,
        server_version: str,
        clock: Callable[[], float],
    ) -> None:
        """Keep the collaborators; the session lists the endpoint's tools."""
        self._endpoint = endpoint
        self._lanes = lanes
        self._outbox = outbox
        self._log = log
        self._shutdown = shutdown
        self._limits = limits
        self._clock = clock
        self._session = Session(server_version=server_version, tools=list(endpoint.tools()))
        self._ids = InFlightIds(maximum=limits.ids_maximum)
        self._held = 0
        self._held_lock = threading.Lock()
        self._queued: dict[MessageId, int] = {}
        self._queued_lock = threading.Lock()
        self._log_failures = 0
        self._output_failed = threading.Event()

    def initialized(self) -> bool:
        """Return whether the client has completed `initialize`."""
        return self._session.initialized()

    def in_flight(self) -> int:
        """Return how many tool calls are admitted and not yet answered."""
        return self._ids.count()

    def output_failed(self) -> bool:
        """Return whether an answer could not be queued: the client stopped reading."""
        return self._output_failed.is_set()

    def on_refused(self, error: FrameError) -> None:
        """Answer a line the splitter dropped before it was complete."""
        self._send(error_message(None, error.code, error.message), priority=True)

    def on_frame(self, data: bytes) -> None:
        """Handle one complete frame; called from the reactor thread only."""
        try:
            message = parse_frame(data, self._limits.frame)
        except FrameError as error:
            self.on_refused(error)
            return
        for action in self._session.handle(classify(message, self._limits.string_bytes)):
            self._act(action, len(data))

    def _act(self, action: Action, size: int) -> None:
        """Carry out what the session decided."""
        if isinstance(action, Reply):
            self._send(action.message, priority=True)
        elif isinstance(action, Log):
            self._note(action.event, action.detail)
        elif isinstance(action, Cancel):
            self._ids.cancel(action.request_id)
        elif isinstance(action, CallTool):
            self._dispatch(action, size)

    def _dispatch(self, call: CallTool, size: int) -> None:
        """Admit a tool call and queue it on its lane, or refuse it at once."""
        admission = self._ids.begin(call.request_id)
        if admission is Admission.DUPLICATE:
            text = "a request with this id is already in flight"
            self._send(error_message(call.request_id, INVALID_REQUEST, text), priority=True)
            return
        if admission is Admission.FULL:
            self._busy(call.request_id, "too many requests are in flight")
            return
        if not self._hold(size):
            self._ids.finish(call.request_id)
            self._busy(call.request_id, "the requests in flight hold too much input")
            return
        lane = Lane.CONTROL if call.name in CONTROL_TOOLS else Lane.NORMAL
        with self._queued_lock:
            self._queued[call.request_id] = size
        if not self._lanes.submit(lane, lambda: self._run(call, lane, size)):
            with self._queued_lock:
                self._queued.pop(call.request_id, None)
            self._release(size)
            self._ids.finish(call.request_id)
            self._busy(call.request_id, f"the {lane.value} lane is full; try again")

    def abandon_queued(self) -> None:
        """Release and answer the calls that were queued but never started.

        Called after the lanes were closed: their jobs were dropped, so they would stay in flight
        for ever. A call that already started is not touched.
        """
        with self._queued_lock:
            abandoned = dict(self._queued)
            self._queued.clear()
        for request_id, size in abandoned.items():
            self._release(size)
            self._ids.finish(request_id)
            outcome = error_outcome("SHUTTING_DOWN", "the server is shutting down")
            self._send(_result_for(request_id, outcome), priority=True)

    def _run(self, call: CallTool, lane: Lane, size: int) -> None:
        """Run one tool call on a lane worker and answer it unless it was cancelled."""
        request_id = call.request_id
        with self._queued_lock:
            if self._queued.pop(request_id, None) is None:
                return
        started = self._clock()
        try:
            outcome = self._endpoint.call(
                call.name,
                call.arguments,
                lambda: self._ids.is_cancelled(request_id) or self._shutdown.is_set(),
            )
        except Exception as error:  # noqa: BLE001 - a tool must never take the server down
            self._note("TOOL_FAILED", f"tool={call.name} error={type(error).__name__}")
            outcome = error_outcome("INTERNAL", "the tool failed unexpectedly")
        cancelled = self._ids.is_cancelled(request_id)
        if not cancelled:
            # The answer is queued while the id still counts as in flight, so a shutdown that
            # waits for in-flight calls cannot close the outbox in front of it.
            self._send(self._tool_message(request_id, outcome), priority=lane is Lane.CONTROL)
        self._ids.finish(request_id)
        self._release(size)
        millis = int((self._clock() - started) * _MS)
        label = "CANCELLED" if cancelled else _outcome_label(outcome)
        self._note("TOOL", f"tool={call.name} lane={lane.value} outcome={label} ms={millis}")
        if cancelled:
            self._note_commit(call.name, outcome)

    def _note(self, event: str, detail: str) -> None:
        """Write one log line; a log that fails must not cost an answer or a worker.

        The failures are counted and reported by `log_failures`, which the transport writes into
        the closing line.
        """
        try:
            self._log.record(event, detail)
        except Exception:  # noqa: BLE001 - the log must never take an answer down with it
            with self._queued_lock:
                self._log_failures += 1

    def log_failures(self) -> int:
        """Return how many log lines could not be written."""
        with self._queued_lock:
            return self._log_failures

    def _note_commit(self, name: str, outcome: ToolOutcome) -> None:
        """Tell the log where a run is when its answer was suppressed by a cancellation."""
        run_id = outcome.payload.get("run_id")
        if name in _COMMITTING_TOOLS and not outcome.is_error and isinstance(run_id, str):
            self._note("CANCELLED_AFTER_COMMIT", f"tool={name} run={run_id}")

    def _tool_message(self, request_id: MessageId, outcome: ToolOutcome) -> dict[str, object]:
        """Build the `tools/call` result; a result over the output bound becomes a small error."""
        try:
            message = _result_for(request_id, outcome)
            if len(encode_frame(message)) <= self._limits.out_bytes:
                return message
        except (TypeError, ValueError, RecursionError) as error:
            self._note("TOOL_FAILED", f"encode error={type(error).__name__}")
            return _result_for(
                request_id, error_outcome("INTERNAL", "the result cannot be encoded")
            )
        text = f"the result is larger than {self._limits.out_bytes} bytes"
        return _result_for(request_id, error_outcome("RESULT_TOO_LARGE", text))

    def _busy(self, request_id: MessageId, text: str) -> None:
        """Answer a call that cannot be taken now; the caller may retry."""
        self._send(_result_for(request_id, error_outcome("BUSY", text)), priority=True)

    def _hold(self, size: int) -> bool:
        """Count the input bytes of an admitted request; False when the bound would be passed."""
        with self._held_lock:
            if self._held + size > self._limits.aggregate_bytes:
                return False
            self._held += size
            return True

    def _release(self, size: int) -> None:
        """Forget the input bytes of a request that is done."""
        with self._held_lock:
            self._held -= size

    def _send(self, message: dict[str, object], *, priority: bool) -> None:
        """Queue an answer; a full or closed outbox ends the server."""
        if self._outbox.put(encode_frame(message), priority=priority):
            return
        self._log.record("DROPPED", "reason=outbox_full")
        self._output_failed.set()
        self._shutdown.set()


def _result_for(request_id: MessageId, outcome: ToolOutcome) -> dict[str, object]:
    """Wrap an outcome as the result of `tools/call`: text for old clients, data for new ones."""
    text = json.dumps(outcome.payload, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return result_message(
        request_id,
        {
            "content": [{"type": "text", "text": text}],
            "structuredContent": outcome.payload,
            "isError": outcome.is_error,
        },
    )


def _outcome_label(outcome: ToolOutcome) -> str:
    """Return OK, or ERROR(code) for a failure, for the log."""
    if not outcome.is_error:
        return "OK"
    error = outcome.payload.get("error")
    code = error.get("code") if isinstance(error, dict) else None
    return f"ERROR({code if isinstance(code, str) else 'UNKNOWN'})"
