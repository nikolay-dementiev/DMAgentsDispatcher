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
