"""JSON-RPC 2.0 as MCP uses it: classify frames, build answers, track ids in flight.

Pure; the registry takes its own lock because the lanes share it.
"""

import enum
import threading
from dataclasses import dataclass

INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

MessageId = int | str
_VERSION = "2.0"
# JSON numbers beyond 2**53 are not exact in many clients; an id must survive a round trip.
_MAX_INTEGER_ID = 2**53


@dataclass(frozen=True)
class Request:
    """A call that expects exactly one answer."""

    id: MessageId
    method: str
    params: dict[str, object]


@dataclass(frozen=True)
class Notification:
    """A message that is never answered."""

    method: str
    params: dict[str, object]


@dataclass(frozen=True)
class Unsolicited:
    """A result or an error nobody asked for: the server sends no requests."""


@dataclass(frozen=True)
class Invalid:
    """A message that cannot be served; `respond` says whether the sender gets an error back."""

    id: MessageId | None
    code: int
    message: str
    respond: bool = True


Incoming = Request | Notification | Unsolicited | Invalid


def classify(message: object, string_bytes: int) -> Incoming:
    """Say what a decoded frame is; ids longer than `string_bytes` (UTF-8) are refused."""
    if isinstance(message, list):
        return Invalid(None, INVALID_REQUEST, "batches are not supported")
    if not isinstance(message, dict):
        return Invalid(None, INVALID_REQUEST, "a message must be a JSON object")
    return _classify_object(message, string_bytes)


def _classify_object(message: dict[str, object], string_bytes: int) -> Incoming:
    """Say what a JSON object is."""
    has_id = "id" in message
    request_id = _readable_id(message.get("id"), string_bytes) if has_id else None
    if has_id and request_id is None:
        return Invalid(None, INVALID_REQUEST, "an id is a string or an integer")
    if message.get("jsonrpc") != _VERSION:
        return Invalid(request_id, INVALID_REQUEST, 'the "jsonrpc" member must be "2.0"')
    method = message.get("method")
    if method is None and ("result" in message or "error" in message):
        return Unsolicited()
    return _classify_call(request_id, method, message.get("params", {}))


def _classify_call(request_id: MessageId | None, method: object, params: object) -> Incoming:
    """Say whether a message with a method is a request, a notification or malformed."""
    if not isinstance(method, str):
        return Invalid(request_id, INVALID_REQUEST, "the method must be a string")
    if not isinstance(params, dict):
        return Invalid(
            request_id, INVALID_PARAMS, "params must be an object", respond=request_id is not None
        )
    if request_id is None:
        return Notification(method, params)
    return Request(request_id, method, params)


def result_message(request_id: MessageId, result: dict[str, object]) -> dict[str, object]:
    """Return the success answer to `request_id`."""
    return {"jsonrpc": _VERSION, "id": request_id, "result": result}


def error_message(request_id: MessageId | None, code: int, text: str) -> dict[str, object]:
    """Return the error answer; `request_id` is None when the request's id was unreadable."""
    return {"jsonrpc": _VERSION, "id": request_id, "error": {"code": code, "message": text}}


def _readable_id(value: object, string_bytes: int) -> MessageId | None:
    """Return the id when it is a string or an integer within bounds, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if abs(value) <= _MAX_INTEGER_ID else None
    if isinstance(value, str):
        return value if len(value.encode("utf-8")) <= string_bytes else None
    return None


class Admission(enum.Enum):
    """The outcome of registering a request id."""

    ADMITTED = enum.auto()
    DUPLICATE = enum.auto()
    FULL = enum.auto()


class InFlightIds:
    """The ids admitted and not yet answered, each with its cancel flag."""

    def __init__(self, *, maximum: int) -> None:
        """Keep the bound on how many ids may be in flight at once."""
        self._maximum = maximum
        self._cancelled: dict[MessageId, bool] = {}
        self._lock = threading.Lock()

    def begin(self, request_id: MessageId) -> Admission:
        """Register an id; refuse a duplicate or an overflow."""
        with self._lock:
            if request_id in self._cancelled:
                return Admission.DUPLICATE
            if len(self._cancelled) >= self._maximum:
                return Admission.FULL
            self._cancelled[request_id] = False
            return Admission.ADMITTED

    def finish(self, request_id: MessageId) -> None:
        """Release an id once its answer is decided."""
        with self._lock:
            self._cancelled.pop(request_id, None)

    def cancel(self, request_id: MessageId) -> bool:
        """Set the cancel flag of an id in flight; return False for an unknown id."""
        with self._lock:
            if request_id not in self._cancelled:
                return False
            self._cancelled[request_id] = True
            return True

    def count(self) -> int:
        """Return how many ids are in flight."""
        with self._lock:
            return len(self._cancelled)

    def is_cancelled(self, request_id: MessageId) -> bool:
        """Return whether the id is in flight and was cancelled."""
        with self._lock:
            return self._cancelled.get(request_id, False)
