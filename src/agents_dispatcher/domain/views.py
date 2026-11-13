"""The public views of a run: summary, status and stop receipt (M2-provisional subsets).

Names follow the architecture draft 4: `RunSummaryV1` and `StatusViewV1` at L1658-L1662 and
`RunStopReceiptV1` at L1081. Fields the M2 cut leaves out are not invented. This module is pure.
"""

from dataclasses import dataclass
from enum import Enum

from agents_dispatcher.domain.run import RunId, RunState, TerminalReason

PROVIDER = "claude-code"
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
    if state is RunState.FINALIZING:
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

    def as_dict(self) -> dict[str, object]:
        """Return the summary as plain values in contract order."""
        return {
            "run_id": self.run_id.value,
            "task": self.task,
            "provider": PROVIDER,
            "state": self.state.name,
            "reason": self.reason.name if self.reason else None,
            "branch": self.branch,
        }


@dataclass(frozen=True)
class StatusView:
    """What a caller sees of a run right now (M2 subset of `StatusViewV1`)."""

    run: RunSummary
    revision: int
    owner_alive: bool
    stop: StopInfo | None

    def as_dict(self) -> dict[str, object]:
        """Return the view as plain values in contract order."""
        return {
            "target": {"run_id": self.run.run_id.value},
            "revision": self.revision,
            "run": self.run.as_dict(),
            "owner_alive": self.owner_alive,
            "stop": self.stop.as_dict() if self.stop else None,
        }
