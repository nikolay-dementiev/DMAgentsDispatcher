"""MCP stdio framing: newline delimited frames, read within bounds, parsed within bounds.

Pure: no I/O and no clock of its own. The caller passes the time of every read.
"""

import json
import re
from dataclasses import dataclass

PARSE_ERROR = -32700
INVALID_REQUEST = -32600

_NUMBER_START = b"-+.0123456789"
_TOKEN = re.compile(
    rb'"[^"\\]*(?:\\.[^"\\]*)*"'  # a whole string, escapes included
    rb"|[{\[]"
    rb"|[}\]]+"  # a run of closers is one step, so the scan cost follows the bytes cheaply
    rb"|[-+.0-9eE]+"  # a number, whatever its form
    rb"|[A-Za-z]+"  # true, false, null, NaN, Infinity
    rb"|[,:\s]+"
    rb"|[^\s\"{}\[\],:A-Za-z0-9+.\-]+",  # a run of stray bytes is one step too
    re.DOTALL,
)


class FrameError(Exception):
    """A frame the server refuses, with the JSON-RPC code to answer with."""

    def __init__(self, code: int, message: str) -> None:
        """Keep the code and the message."""
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class FrameLimits:
    """What one frame may hold."""

    in_bytes: int
    depth: int
    nodes: int
    numeric_token_bytes: int


@dataclass(frozen=True)
class Frame:
    """One complete line, without its newline."""

    data: bytes


@dataclass(frozen=True)
class Refused:
    """A line that was dropped before it was complete."""

    error: FrameError


class FrameSplitter:
    """Cuts a byte stream into lines, counting bytes as they arrive."""

    def __init__(self, *, in_bytes: int, partial_timeout_s: float) -> None:
        """Keep the byte bound and the time a partial frame may stand still."""
        self._in_bytes = in_bytes
        self._partial_timeout_s = partial_timeout_s
        self._buffer = bytearray()
        self._discarding = False
        self._last_byte_at = 0.0

    def buffered(self) -> int:
        """Return the number of bytes held for an unfinished line."""
        return len(self._buffer)

    def feed(self, chunk: bytes, now: float) -> list[Frame | Refused]:
        """Take bytes read at `now`; return the lines and refusals they complete."""
        events: list[Frame | Refused] = []
        if chunk:
            self._last_byte_at = now
        rest = chunk
        while rest:
            newline = rest.find(b"\n")
            piece = rest if newline < 0 else rest[:newline]
            rest = b"" if newline < 0 else rest[newline + 1 :]
            self._take(piece, complete=newline >= 0, events=events)
        return events

    def poll(self, now: float) -> list[Refused]:
        """Drop a partial frame that has not grown for the allowed time."""
        if self._discarding or not self._buffer:
            return []
        if now - self._last_byte_at < self._partial_timeout_s:
            return []
        self._buffer.clear()
        self._discarding = True
        message = f"a frame stood still for {self._partial_timeout_s:g} s and was dropped"
        return [Refused(FrameError(INVALID_REQUEST, message))]

    def _take(self, piece: bytes, *, complete: bool, events: list[Frame | Refused]) -> None:
        """Add one piece of a line; `complete` says a newline followed it."""
        if self._discarding:
            self._discarding = not complete
            return
        if len(self._buffer) + len(piece) > self._in_bytes:
            self._buffer.clear()
            self._discarding = not complete
            message = f"a frame is longer than {self._in_bytes} bytes and was dropped"
            events.append(Refused(FrameError(INVALID_REQUEST, message)))
            return
        self._buffer.extend(piece)
        if complete:
            line = bytes(self._buffer)
            self._buffer.clear()
            if line.strip():
                events.append(Frame(line))


def encode_frame(message: dict[str, object]) -> bytes:
    """Return one outgoing frame: compact ASCII JSON and a newline, never an embedded newline."""
    text = json.dumps(message, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return text.encode("ascii") + b"\n"


def parse_frame(data: bytes, limits: FrameLimits) -> object:
    """Decode one frame; raise `FrameError` for anything outside the bounds or not JSON.

    The bytes are scanned for depth, node count and numeric token length before the parser sees
    them, so a hostile frame inside the byte bound cannot make the parser do unbounded work.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise FrameError(PARSE_ERROR, "the frame is not valid UTF-8") from error
    if "\x00" in text:
        # json.loads would take NUL bytes for UTF-16 or UTF-32 and read other text than we scanned.
        raise FrameError(PARSE_ERROR, "the frame holds a NUL character")
    _check_bounds(data, limits)
    try:
        return json.loads(
            text,
            object_pairs_hook=_no_duplicates,
            parse_constant=_reject_constant,
        )
    except FrameError:
        raise
    except (ValueError, RecursionError) as error:
        raise FrameError(PARSE_ERROR, f"the frame is not valid JSON: {error}") from error


def _check_bounds(data: bytes, limits: FrameLimits) -> None:
    """Walk the tokens once; refuse a frame that is too deep, too big or has a long number."""
    depth = 0
    nodes = 0
    for match in _TOKEN.finditer(data):
        token = match.group()
        first = token[:1]
        if first in b"{[":
            depth += 1
            nodes += 1
            if depth > limits.depth:
                raise FrameError(INVALID_REQUEST, f"nesting is deeper than {limits.depth}")
        elif first in b"}]":
            depth = max(0, depth - len(token))
        elif first == b'"':
            _check_string(token)
            nodes += 1
        elif first in _NUMBER_START:
            _check_number(token, limits)
            nodes += 1
        elif first.isalpha():
            nodes += 1
        if nodes > limits.nodes:
            raise FrameError(INVALID_REQUEST, f"more than {limits.nodes} values in one frame")


def _check_string(token: bytes) -> None:
    """Refuse a lone quote: the string it opens never closes."""
    if len(token) < _MIN_STRING or not token.endswith(b'"'):
        raise FrameError(PARSE_ERROR, "a string is not terminated")


def _check_number(token: bytes, limits: FrameLimits) -> None:
    """Refuse a numeric token longer than the bound, whatever its form."""
    if len(token) > limits.numeric_token_bytes:
        message = f"a number is longer than {limits.numeric_token_bytes} characters"
        raise FrameError(INVALID_REQUEST, message)


_MIN_STRING = 2  # the two quotes of an empty string


def _no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build an object, refusing a key that appears twice."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise FrameError(INVALID_REQUEST, f"the key {key!r} appears twice in one object")
        result[key] = value
    return result


def _reject_constant(name: str) -> object:
    """Refuse NaN and the infinities, which JSON does not have."""
    raise FrameError(PARSE_ERROR, f"{name} is not a JSON value")
