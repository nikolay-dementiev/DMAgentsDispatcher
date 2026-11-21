"""Spies for the ports of the quarantine resolution; every call lands in one shared journal."""
# ruff: noqa: ARG002 - spies must match the port signatures even where they ignore an argument

from collections.abc import Sequence
from dataclasses import dataclass, field

from agents_dispatcher.application.errors import WorkspaceError
from agents_dispatcher.application.ports import (
    AbsenceResult,
    FinishRecord,
    HeldRun,
    SealedResult,
)
from agents_dispatcher.domain.routing import TaskMode
from agents_dispatcher.domain.run import RunId, RunState, Survivor, TerminalReason
from tests.support.run_spies import RUN_ID, Journal

STAMP = "2026-11-21T10:00:00Z"
GONE = AbsenceResult(empty=True, survivors=())
ALIVE = AbsenceResult(empty=False, survivors=(Survivor(901, 12.5, ("group",)),))
KEPT = FinishRecord(
    terminal_reason=TerminalReason.COMPLETED,
    branch=None,
    commits_on_branch=0,
    limit_hit=None,
    worker_exit_status=0,
    duration_s=9.5,
    report_json="REPORT",
)


def held_run(
    state: RunState = RunState.QUARANTINED,
    *,
    mode: TaskMode = TaskMode.WRITE,
    released_at: float | None = None,
    verified_at: float | None = None,
) -> HeldRun:
    """Build the default run as the ledger shows it."""
    return HeldRun(
        run_id=RUN_ID,
        state=state,
        repo="/work/example",
        base_commit="b" * 40,
        mode=mode,
        released_at=released_at,
        absence_verified_at=verified_at,
    )


@dataclass
class SpyLedger:
    """A ledger over one dictionary that records what was settled, released and verified."""

    journal: Journal
    runs: dict[RunId, HeldRun] = field(default_factory=dict)
    kept: dict[RunId, FinishRecord] = field(default_factory=dict)
    waiting: list[RunId] = field(default_factory=list)
    settled: list[tuple[FinishRecord, float]] = field(default_factory=list)
    released: list[tuple[FinishRecord, str, float]] = field(default_factory=list)
    verified: list[float] = field(default_factory=list)

    def held(self, run_id: RunId) -> HeldRun | None:
        """Return the run or None."""
        return self.runs.get(run_id)

    def pending(self, run_id: RunId) -> FinishRecord | None:
        """Return the kept outcome or None."""
        return self.kept.get(run_id)

    def settle(self, run_id: RunId, record: FinishRecord, *, verified_at: float) -> None:
        """Record the settlement."""
        self.journal.add(f"ledger.settle {record.terminal_reason.name}")
        self.settled.append((record, verified_at))

    def release(
        self, run_id: RunId, record: FinishRecord, *, note: str, released_at: float
    ) -> None:
        """Record the release."""
        self.journal.add("ledger.release")
        self.released.append((record, note, released_at))

    def mark_verified(self, run_id: RunId, *, verified_at: float) -> None:
        """Record that absence was verified for a released run."""
        self.journal.add("ledger.verified")
        self.verified.append(verified_at)

    def awaiting(self) -> list[RunId]:
        """Return the runs a re-check should look at."""
        return list(self.waiting)


@dataclass
class SpyLook:
    """An absence look with a script of answers; the last one repeats."""

    journal: Journal
    answers: list[AbsenceResult | Exception] = field(default_factory=lambda: [GONE])

    def look(self, run_id: RunId) -> AbsenceResult:
        """Play the next answer."""
        self.journal.add("look")
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return answer


@dataclass
class SpyKept:
    """A kept workspace that seals to a canned result and remembers the removal."""

    journal: Journal
    sealed: SealedResult = field(default_factory=lambda: SealedResult("agentsd-run-x", 1))
    seal_error: WorkspaceError | None = None
    remove_error: OSError | None = None

    def seal(self, run: HeldRun) -> SealedResult:
        """Seal or fail."""
        self.journal.add("kept.seal")
        if self.seal_error is not None:
            raise self.seal_error
        return self.sealed

    def remove(self, run: HeldRun) -> None:
        """Remove or fail."""
        self.journal.add("kept.remove")
        if self.remove_error is not None:
            raise self.remove_error


class SpyAmender:
    """An amender that shows what it was told in a plain string."""

    def amend(
        self, report_json: str, *, branch: str | None, commits: int, notes: Sequence[str]
    ) -> str:
        """Return the arguments joined, so a test can read them."""
        return f"{report_json}|{branch}|{commits}|{'/'.join(notes)}"


@dataclass
class SpyEvents:
    """The SDK-log lines of the resolution."""

    lines: list[str] = field(default_factory=list)

    def record(self, line: str) -> None:
        """Keep one line."""
        self.lines.append(line)
