"""In-memory stand-ins for the read side of the run store, used by the M2 use case tests."""

from collections.abc import Callable
from dataclasses import dataclass, field, replace

from agents_dispatcher.application.ports import RunSnapshot
from agents_dispatcher.domain.run import RunId, RunState, TerminalReason
from agents_dispatcher.domain.views import StopDisposition
from tests.support.clock import FakeClock
from tests.support.routing import ROUTING
from tests.support.run_spies import RUN_ID

MAX_SLEEPS = 100
REPORT_JSON = '{"run_id": "3f2b8c1e-5d4a-4b7e-9c3a-1f2e3d4c5b6a", "state": "TERMINAL"}'


def snapshot(
    state: RunState = RunState.RUNNING,
    *,
    task: str = "fix the bug",
    reason: TerminalReason | None = None,
    report_json: str | None = None,
) -> RunSnapshot:
    """Build a snapshot of the default test run."""
    return RunSnapshot(
        run_id=RUN_ID,
        task=task,
        state=state,
        terminal_reason=reason,
        branch=RUN_ID.branch_name if reason is TerminalReason.COMPLETED else None,
        revision=1,
        stop=None,
        report_json=report_json,
        fault=None,
        routing=ROUTING,
    )


@dataclass
class RunBook:
    """A reader, a stop registry, an owner probe and an orphan closer over one dictionary."""

    runs: dict[RunId, RunSnapshot] = field(default_factory=dict)
    owner_alive_for: set[RunId] = field(default_factory=set)
    stop_answer: StopDisposition | None = StopDisposition.EFFECTIVE
    stop_calls: list[tuple[RunId, str]] = field(default_factory=list)
    close_calls: list[RunId] = field(default_factory=list)
    closes_with: RunSnapshot | None = None

    def find(self, run_id: RunId) -> RunSnapshot | None:
        """Return the stored snapshot or None."""
        return self.runs.get(run_id)

    def request_stop(self, run_id: RunId, reason: str) -> StopDisposition | None:
        """Record the call and answer with the canned disposition (None for an unknown run)."""
        self.stop_calls.append((run_id, reason))
        return self.stop_answer if run_id in self.runs else None

    def owner_alive(self, run_id: RunId) -> bool:
        """Return whether the run has a live owner."""
        return run_id in self.owner_alive_for

    def close_if_orphaned(self, run_id: RunId) -> bool:
        """Record the call; close the run when a closing snapshot is configured."""
        self.close_calls.append(run_id)
        if self.closes_with is None:
            return False
        self.runs[run_id] = replace(self.closes_with, run_id=run_id)
        return True


class FakeSleeper:
    """A sleeper that moves a fake clock, optionally running a hook at each sleep."""

    def __init__(self, clock: FakeClock, on_sleep: Callable[[int], None] | None = None) -> None:
        """Remember the clock and the hook."""
        self.clock = clock
        self.on_sleep = on_sleep
        self.calls: list[float] = []

    def sleep(self, seconds: float) -> None:
        """Record the request, advance the clock and run the hook with the call number."""
        self.calls.append(seconds)
        if len(self.calls) > MAX_SLEEPS:
            message = "runaway wait loop: the use case never stopped sleeping"
            raise AssertionError(message)
        self.clock.advance(seconds)
        if self.on_sleep is not None:
            self.on_sleep(len(self.calls))
