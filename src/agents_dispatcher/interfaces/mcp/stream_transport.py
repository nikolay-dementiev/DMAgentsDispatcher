"""The MCP transport over byte streams: read, split, serve, write, shut down in order.

The loop knows nothing of file descriptors: bytes come from a `ByteInput` and go to a
`ByteOutput`. The adapters that wrap real descriptors live in the adapter layer.
"""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from agents_dispatcher.application.lanes import LaneRunner
from agents_dispatcher.interfaces.mcp.framing import Frame, FrameSplitter, Refused
from agents_dispatcher.interfaces.mcp.outbox import Outbox
from agents_dispatcher.interfaces.mcp.ports import (
    ByteInput,
    ByteOutput,
    McpEventLog,
    ToolEndpoint,
)
from agents_dispatcher.interfaces.mcp.server import McpServer, ServerLimits

EXIT_OK = 0
EXIT_FAILURE = 1
_CLEAN_ENDS = frozenset({"EOF", "SIGNAL"})


@dataclass(frozen=True)
class Timings:
    """The clocks of the loop, in seconds."""

    initialize_timeout_s: float
    partial_frame_timeout_s: float
    write_timeout_s: float
    shutdown_drain_s: float
    shutdown_total_s: float
    poll_s: float


class StreamTransport:
    """Serves one client over a pair of byte streams."""

    def __init__(  # noqa: PLR0913 - the collaborators of a transport, injected by its composition
        self,
        *,
        input: ByteInput,  # noqa: A002 - the port is named after what it is
        output: ByteOutput,
        lanes: LaneRunner,
        limits: ServerLimits,
        timings: Timings,
        log: McpEventLog,
        server_version: str,
        outbox_bytes: int,
        priority_burst: int,
        clock: Callable[[], float],
    ) -> None:
        """Keep the collaborators."""
        self._input = input
        self._output = output
        self._lanes = lanes
        self._limits = limits
        self._timings = timings
        self._log = log
        self._server_version = server_version
        self._outbox_bytes = outbox_bytes
        self._priority_burst = priority_burst
        self._clock = clock

    def serve(self, endpoint: ToolEndpoint, shutdown: threading.Event) -> int:
        """Serve until the input ends or `shutdown` is set; return the process exit code."""
        outbox = Outbox(max_bytes=self._outbox_bytes, priority_burst=self._priority_burst)
        server = McpServer(
            endpoint=endpoint,
            lanes=self._lanes,
            outbox=outbox,
            log=self._log,
            shutdown=shutdown,
            limits=self._limits,
            server_version=self._server_version,
            clock=self._clock,
        )
        writer_failed = threading.Event()
        writer = threading.Thread(
            target=self._write_loop, args=(outbox, shutdown, writer_failed), daemon=True
        )
        writer.start()
        self._log.record("SERVE_START", f"transport=own version={self._server_version}")
        reason = self._read_loop(server, shutdown, writer_failed)
        self._shut_down(server, outbox, writer, shutdown)
        self._log.record("SERVE_END", f"reason={reason} log_failures={server.log_failures()}")
        return EXIT_OK if reason in _CLEAN_ENDS else EXIT_FAILURE

    def _read_loop(
        self, server: McpServer, shutdown: threading.Event, writer_failed: threading.Event
    ) -> str:
        """Feed frames to the server; return why reading stopped."""
        splitter = FrameSplitter(
            in_bytes=self._limits.frame.in_bytes,
            partial_timeout_s=self._timings.partial_frame_timeout_s,
        )
        started = self._clock()
        while True:
            if writer_failed.is_set() or server.output_failed():
                return "OUTPUT_FAILURE"
            if shutdown.is_set():
                return "SIGNAL"
            now = self._clock()
            if not server.initialized() and now - started >= self._timings.initialize_timeout_s:
                return "INIT_TIMEOUT"
            chunk = self._input.read(self._timings.poll_s)
            if chunk == b"":
                return "EOF"
            events: list[Frame | Refused] = (
                [*splitter.poll(now)] if chunk is None else splitter.feed(chunk, now)
            )
            for event in events:
                if isinstance(event, Frame):
                    server.on_frame(event.data)
                else:
                    server.on_refused(event.error)

    def _write_loop(
        self, outbox: Outbox, shutdown: threading.Event, failed: threading.Event
    ) -> None:
        """Write queued frames until the outbox is closed and empty, or a write fails."""
        while True:
            frame = outbox.get(timeout_s=self._timings.poll_s)
            if frame is None:
                if outbox.drained():
                    return
                continue
            if not self._output.write(frame, self._timings.write_timeout_s):
                failed.set()
                shutdown.set()
                return

    def _shut_down(
        self,
        server: McpServer,
        outbox: Outbox,
        writer: threading.Thread,
        shutdown: threading.Event,
    ) -> None:
        """Stop admission, let finished replies drain, and leave within the total bound."""
        deadline = time.monotonic() + self._timings.shutdown_total_s
        shutdown.set()
        self._lanes.close()
        server.abandon_queued()
        drain_until = time.monotonic() + self._timings.shutdown_drain_s
        while server.in_flight() > 0 and time.monotonic() < drain_until:
            time.sleep(self._timings.poll_s)
        outbox.close()
        writer.join(max(0.0, deadline - time.monotonic()))
