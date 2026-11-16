"""The seams of the MCP front end: what a transport needs, and what it serves."""

import threading
from collections.abc import Callable, Sequence
from typing import Protocol

from agents_dispatcher.application.tool_outcome import ToolOutcome


class ToolEndpoint(Protocol):
    """The tools a server offers. It knows nothing of JSON-RPC or of any transport."""

    def tools(self) -> Sequence[dict[str, object]]:
        """Return the tool descriptions as `tools/list` shows them."""
        ...

    def call(self, name: str, arguments: object, cancelled: Callable[[], bool]) -> ToolOutcome:
        """Run a tool; `cancelled` says the caller no longer wants the answer."""
        ...


class McpTransport(Protocol):
    """Carries MCP messages between a client and a `ToolEndpoint`."""

    def serve(self, endpoint: ToolEndpoint, shutdown: threading.Event) -> int:
        """Serve until the client goes away or `shutdown` is set; return the exit code."""
        ...


class McpEventLog(Protocol):
    """Where the server writes its one-line events (the private SDK log)."""

    def record(self, event: str, detail: str) -> None:
        """Write one event; `detail` never holds arguments, task text or an account."""
        ...


class ByteInput(Protocol):
    """Where the client's bytes come from."""

    def read(self, timeout_s: float) -> bytes | None:
        """Return the next chunk, b"" at the end of the input, None when none came in time."""
        ...


class ByteOutput(Protocol):
    """Where the server's bytes go."""

    def write(self, data: bytes, timeout_s: float) -> bool:
        """Write all of `data`; return False when it could not be written in time."""
        ...
