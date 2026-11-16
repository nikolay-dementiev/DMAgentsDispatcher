"""The MCP lifecycle as a state machine: what the server does with each classified message.

Pure: the session decides, the transport acts. It never runs a tool and never writes.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass

from agents_dispatcher.interfaces.mcp.protocol import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    Incoming,
    Invalid,
    MessageId,
    Notification,
    Request,
    Unsolicited,
    error_message,
    result_message,
)

SUPPORTED_VERSIONS = ("2025-06-18", "2025-11-25")
LATEST_VERSION = SUPPORTED_VERSIONS[-1]
SERVER_NAME = "agents-dispatcher"
_LOG_TEXT_CHARS = 64
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")


@dataclass(frozen=True)
class Reply:
    """Write this message now."""

    message: dict[str, object]


@dataclass(frozen=True)
class CallTool:
    """Run a tool; the arguments are whatever the client sent and are judged by the tool."""

    request_id: MessageId
    name: str
    arguments: object


@dataclass(frozen=True)
class Cancel:
    """Set the cancel flag of a request that may be in flight."""

    request_id: MessageId


@dataclass(frozen=True)
class Log:
    """Write one line to the SDK log."""

    event: str
    detail: str


Action = Reply | CallTool | Cancel | Log


class Session:
    """One connection's lifecycle."""

    def __init__(self, *, server_version: str, tools: Sequence[dict[str, object]]) -> None:
        """Keep the version to announce and the tool descriptions to list."""
        self._server_version = server_version
        self._tools = list(tools)
        self._names = {str(tool["name"]) for tool in tools}
        self._initialized = False
        self._saw_initialized_notice = False
        self._early_call_noted = False

    def initialized(self) -> bool:
        """Return whether the `initialize` request was answered."""
        return self._initialized

    def handle(self, incoming: Incoming) -> list[Action]:
        """Decide what to do with one classified message."""
        if isinstance(incoming, Invalid):
            return self._refuse(incoming)
        if isinstance(incoming, Unsolicited):
            return [Log("DROPPED", "reason=unsolicited_response")]
        if isinstance(incoming, Notification):
            return self._notification(incoming)
        return self._request(incoming)

    def _refuse(self, invalid: Invalid) -> list[Action]:
        """Answer a malformed message when the sender can be told, else note the drop."""
        if invalid.respond:
            return [Reply(error_message(invalid.id, invalid.code, invalid.message))]
        return [Log("DROPPED", "reason=malformed_notification")]

    def _notification(self, notification: Notification) -> list[Action]:
        """Notifications are never answered; only two of them change anything."""
        if not self._initialized:
            return []
        if notification.method == "notifications/initialized":
            self._saw_initialized_notice = True
        elif notification.method == "notifications/cancelled":
            request_id = notification.params.get("requestId")
            if isinstance(request_id, int | str) and not isinstance(request_id, bool):
                return [Cancel(request_id)]
        return []

    def _request(self, request: Request) -> list[Action]:
        """Answer a request; before `initialize` only `initialize` and `ping` are served."""
        if request.method == "ping":
            return [Reply(result_message(request.id, {}))]
        if request.method == "initialize":
            return self._initialize(request)
        if not self._initialized:
            return [Reply(error_message(request.id, INVALID_REQUEST, "server not initialized"))]
        if request.method == "tools/list":
            return self._list_tools(request)
        if request.method == "tools/call":
            return self._call_tool(request)
        text = f"method not found: {_short(request.method)}"
        return [Reply(error_message(request.id, METHOD_NOT_FOUND, text))]

    def _initialize(self, request: Request) -> list[Action]:
        """Negotiate the version and open the session."""
        if self._initialized:
            return [Reply(error_message(request.id, INVALID_REQUEST, "already initialized"))]
        requested = request.params.get("protocolVersion")
        if not isinstance(requested, str):
            text = "protocolVersion is required and must be a string"
            return [Reply(error_message(request.id, INVALID_PARAMS, text))]
        version = requested if requested in SUPPORTED_VERSIONS else LATEST_VERSION
        self._initialized = True
        result: dict[str, object] = {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": self._server_version},
        }
        client = _client_label(request.params.get("clientInfo"))
        return [
            Reply(result_message(request.id, result)),
            Log("INITIALIZED", f"client={client} protocol={version}"),
        ]

    def _list_tools(self, request: Request) -> list[Action]:
        """List the tools; no cursor was ever issued, so none is accepted."""
        if "cursor" in request.params:
            text = "cursor is not accepted: the list is never paged"
            return [Reply(error_message(request.id, INVALID_PARAMS, text))]
        return [Reply(result_message(request.id, {"tools": self._tools}))]

    def _call_tool(self, request: Request) -> list[Action]:
        """Hand a call to a known tool over to the transport."""
        name = request.params.get("name")
        if not isinstance(name, str):
            return [Reply(error_message(request.id, INVALID_PARAMS, "name must be a string"))]
        if name not in self._names:
            return [
                Reply(error_message(request.id, INVALID_PARAMS, f"unknown tool: {_short(name)!r}"))
            ]
        actions: list[Action] = []
        if not self._saw_initialized_notice and not self._early_call_noted:
            self._early_call_noted = True
            actions.append(Log("EARLY_CALL", "tools/call arrived before notifications/initialized"))
        actions.append(CallTool(request.id, name, request.params.get("arguments", {})))
        return actions


def _client_label(info: object) -> str:
    """Return `name/version` of the client for the log, cleaned of anything but short text."""
    if not isinstance(info, dict):
        return "unknown/unknown"
    return f"{_clean(info.get('name'))}/{_clean(info.get('version'))}"


def _clean(value: object) -> str:
    """Return a short single-line text, or `unknown` for anything that is not text."""
    if not isinstance(value, str) or not value:
        return "unknown"
    return _CONTROL.sub("_", value)[:_LOG_TEXT_CHARS]


_ECHO_CHARACTERS = 64


def _short(text: str) -> str:
    """Return client text cut to a length that is safe to echo in an error message."""
    return text if len(text) <= _ECHO_CHARACTERS else text[:_ECHO_CHARACTERS] + "..."
