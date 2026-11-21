"""The public views of a run: summary, status and stop receipt (M2-provisional subsets).

Names follow the architecture draft 4: `RunSummaryV1` and `StatusViewV1` at L1658-L1662 and
`RunStopReceiptV1` at L1081. Fields the M2 cut leaves out are not invented. This module is pure.
"""

from dataclasses import dataclass
from enum import Enum

from agents_dispatcher.domain.routing import RunRouting
from agents_dispatcher.domain.run import RunId, RunState, Survivor, TerminalReason

# C0 and C1 controls, DEL, and the format characters that hide or reorder text on a terminal
# (soft hyphen, Arabic letter mark, zero-width and bidi marks and overrides, word joiner and
# invisible operators, the isolates, the byte order mark, line and paragraph separators, tag
# characters). Listed by hand: the domain layer may
# not import `unicodedata`.
_UNSAFE_RANGES = (
    (0x00, 0x1F),
    (0x7F, 0x9F),
    (0xAD, 0xAD),
    (0x61C, 0x61C),
    (0x200B, 0x200F),
    (0x2028, 0x202E),
    (0x2060, 0x2064),
    (0x2066, 0x206F),
    (0xD800, 0xDFFF),  # lone surrogates cannot be encoded by a strict stream
    (0xFEFF, 0xFEFF),
    (0xFFF9, 0xFFFC),  # interlinear annotation anchors and the object replacement character
    (0xE0000, 0xE007F),  # tag characters: invisible, can carry hidden text
)
TRUNCATION_MARKER = "..."


class StopDisposition(Enum):
    """What a stop request met (ARCH L1081)."""

    EFFECTIVE = "EFFECTIVE"
    ALREADY_STOPPING = "ALREADY_STOPPING"
    TOO_LATE = "TOO_LATE"
    TERMINAL = "TERMINAL"


def stop_disposition(state: RunState, *, already_requested: bool) -> StopDisposition:
    """Decide what a stop request means for a run in `state` (pure; the store applies it)."""
    if state is RunState.TERMINAL:
        return StopDisposition.TERMINAL
    if state in (RunState.FINALIZING, RunState.QUARANTINED):
        return StopDisposition.TOO_LATE
    if already_requested or state is RunState.STOPPING:
        return StopDisposition.ALREADY_STOPPING
    return StopDisposition.EFFECTIVE


def bounded_text(text: str, max_bytes: int) -> str:
    """Return `text`, or its UTF-8 prefix plus a marker, never longer than `max_bytes` bytes."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    room = max(0, max_bytes - len(TRUNCATION_MARKER))
    return encoded[:room].decode("utf-8", errors="ignore") + TRUNCATION_MARKER


def _escape(code: int) -> str:
    """Return the visible escape of one code point."""
    if code < 0x100:  # noqa: PLR2004 - the width of the short form
        return f"\\x{code:02x}"
    if code < 0x10000:  # noqa: PLR2004 - the width of the medium form
        return f"\\u{code:04x}"
    return f"\\U{code:08x}"


def escape_controls(text: str) -> str:
    """Return `text` with control and format characters replaced by visible escapes.

    A character below 256 becomes a backslash, `x` and two hex digits; one up to U+FFFF becomes a
    backslash, `u` and four; a higher one becomes a backslash, `U` and eight.

    Used for every line printed for a human: text that came from a run (a fault, a note, the
    worker's answer) must not move the cursor, rewrite the screen or reorder what is read
    (ARCH L2842). JSON output needs no help: its encoder already escapes controls.
    """
    out: list[str] = []
    for char in text:
        code = ord(char)
        if any(low <= code <= high for low, high in _UNSAFE_RANGES):
            out.append(_escape(code))
        else:
            out.append(char)
    return "".join(out)


def validate_reason(reason: str, max_bytes: int) -> str:
    """Return the stop reason when it is not blank and fits `max_bytes` bytes; else raise."""
    if not reason.strip():
        message = "reason must not be blank"
        raise ValueError(message)
    size = len(reason.encode("utf-8"))
    if size > max_bytes:
        message = f"reason is {size} bytes, over the limit of {max_bytes} (public.reason_bytes)"
        raise ValueError(message)
    return reason


@dataclass(frozen=True)
class StopInfo:
    """A recorded stop request."""

    requested_at: float
    reason: str

    def as_dict(self) -> dict[str, object]:
        """Return the request as plain values."""
        return {"requested_at": self.requested_at, "reason": self.reason}


@dataclass(frozen=True)
class StopReceipt:
    """The answer to a stop request."""

    run_id: RunId
    disposition: StopDisposition

    def as_dict(self) -> dict[str, object]:
        """Return the receipt as plain values; the cause is not tracked in M2."""
        return {
            "run_id": self.run_id.value,
            "disposition": self.disposition.value,
            "primary_cause": None,
        }


@dataclass(frozen=True)
class RunSummary:
    """One run in a line (M2 subset of `RunSummaryV1`)."""

    run_id: RunId
    task: str
    state: RunState
    reason: TerminalReason | None
    branch: str | None
    routing: RunRouting

    def as_dict(self) -> dict[str, object]:
        """Return the summary as plain values in contract order."""
        return {
            "run_id": self.run_id.value,
            "task": self.task,
            "task_type": self.routing.task_type,
            "mode": self.routing.mode.value,
            "provider": self.routing.provider,
            "state": self.state.name,
            "reason": self.reason.name if self.reason else None,
            "branch": self.branch,
        }


QUARANTINE_PROBLEM = "CLEANUP_UNCERTAIN"


@dataclass(frozen=True)
class QuarantineInfo:
    """Why a run is held back: when it was, and the processes that could not be shown gone."""

    since: float
    survivors: tuple[Survivor, ...]

    def as_dict(self, run_id: RunId) -> dict[str, object]:
        """Return the record as plain values, with the command that resolves it."""
        return {
            "problem": QUARANTINE_PROBLEM,
            "since": self.since,
            "survivors": [
                {"pid": item.pid, "start_time": item.start_time, "sources": list(item.sources)}
                for item in self.survivors
            ],
            "action": f"agentsd quarantine resolve {run_id.value}",
        }


@dataclass(frozen=True)
class StatusView:
    """What a caller sees of a run right now (M2 subset of `StatusViewV1`)."""

    run: RunSummary
    revision: int
    owner_alive: bool
    stop: StopInfo | None
    quarantine: QuarantineInfo | None = None

    def as_dict(self) -> dict[str, object]:
        """Return the view as plain values in contract order.

        The `quarantine` member exists only while the run is held back.
        """
        shown: dict[str, object] = {
            "target": {"run_id": self.run.run_id.value},
            "revision": self.revision,
            "run": self.run.as_dict(),
            "route": [item.as_dict() for item in self.run.routing.route],
            "owner_alive": self.owner_alive,
            "stop": self.stop.as_dict() if self.stop else None,
        }
        if self.quarantine is not None:
            shown["quarantine"] = self.quarantine.as_dict(self.run.run_id)
        return shown
