"""Fakes and builders for the command-line tests: services that answer from a script."""

import io
import json
from collections.abc import Callable, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from types import TracebackType

from agents_dispatcher.application.run_queries import ReportResult, WaitResult
from agents_dispatcher.application.services import Services
from agents_dispatcher.domain.routing import Recommendation
from agents_dispatcher.domain.run import (
    LimitKind,
    Limits,
    RunId,
    RunReport,
    RunState,
    TaskSpec,
    TerminalReason,
)
from agents_dispatcher.domain.views import (
    RunSummary,
    StatusView,
    StopDisposition,
    StopInfo,
    StopReceipt,
)
from agents_dispatcher.interfaces.cli.app import main
from agents_dispatcher.interfaces.cli.services import InterruptFlag
from tests.support.routing import ROUTING
from tests.support.run_spies import BASE, RUN_ID


def make_report(reason: TerminalReason, *, limit: LimitKind | None = None) -> RunReport:
    """Build a terminal report with the given reason."""
    return RunReport(
        run_id=RUN_ID,
        state=RunState.TERMINAL,
        terminal_reason=reason,
        repo="/work/example",
        base_commit=BASE,
        branch=RUN_ID.branch_name if reason is TerminalReason.COMPLETED else None,
        commits_on_branch=1 if reason is TerminalReason.COMPLETED else 0,
        worker_cli_version="2.1.304",
        worker_model="claude-sonnet-5-5",
        worker_model_source="reported",
        worker_exit_status=0,
        duration_s=1.5,
        limits_configured=Limits(hard_s=60.0, memory_bytes=1024),
        limit_hit=limit,
        survivors=(),
        result_text="the worker said this",
        notes=("uncommitted changes in the repository are not included",),
        routing=ROUTING,
    )


def report_text(report: RunReport) -> str:
    """Return the stored text form of a report, as the store keeps it."""
    return json.dumps(report.as_dict(), indent=2)


def make_summary(
    state: RunState = RunState.STARTING, reason: TerminalReason | None = None
) -> RunSummary:
    """Build a run summary."""
    return RunSummary(
        run_id=RUN_ID,
        task="add a function",
        state=state,
        reason=reason,
        branch=None,
        routing=ROUTING,
    )


def make_status(
    state: RunState = RunState.RUNNING,
    reason: TerminalReason | None = None,
    stop: StopInfo | None = None,
) -> StatusView:
    """Build a status view."""
    return StatusView(run=make_summary(state, reason), revision=3, owner_alive=True, stop=stop)


def ended(reason: TerminalReason) -> WaitResult:
    """Return a wait result for a run that ended with `reason`."""
    return WaitResult(ready=True, status=make_status(RunState.TERMINAL, reason))


PENDING = WaitResult(ready=False, status=make_status())
Step = WaitResult | Exception | Callable[[], WaitResult]


class FixedVersion:
    """A version source for tests that never read the version."""

    def installed_version(self) -> str | None:
        """Return a fixed version."""
        return "9.9.9-test"


class Flag:
    """An interrupt flag the test raises by hand."""

    def __init__(self) -> None:
        """Start lowered."""
        self.value = False
        self.signals = 0

    def raised(self) -> bool:
        """Say whether the interrupt arrived."""
        return self.value or self.signals > 0

    def count(self) -> int:
        """Say how many interrupts arrived; `value` alone stands for one."""
        return max(self.signals, 1 if self.value else 0)


class Scope(AbstractContextManager[InterruptFlag]):
    """An interrupt scope that hands out one flag and counts how often it was entered."""

    def __init__(self) -> None:
        """Create the flag."""
        self.flag = Flag()
        self.entered = 0
        self.left = 0

    def __call__(self) -> "Scope":
        """Return the context manager."""
        return self

    def __enter__(self) -> InterruptFlag:
        """Count the entry and return the flag."""
        self.entered += 1
        return self.flag

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Count the exit."""
        self.left += 1


@dataclass
class FakeStart:
    """Answers `start` with a summary or raises."""

    result: RunSummary | Recommendation = field(default_factory=make_summary)
    error: BaseException | None = None
    specs: list[TaskSpec] = field(default_factory=list)
    probes: list[Callable[[], bool] | None] = field(default_factory=list)

    def start(
        self, spec: TaskSpec, should_stop: Callable[[], bool] | None = None
    ) -> RunSummary | Recommendation:
        """Record the specification and the stop probe, then raise or answer."""
        self.specs.append(spec)
        self.probes.append(should_stop)
        if self.error is not None:
            raise self.error
        return self.result


@dataclass
class FakeStatus:
    """Answers `status` with a view or raises."""

    result: StatusView = field(default_factory=make_status)
    error: Exception | None = None
    asked: list[RunId] = field(default_factory=list)

    def status(self, run_id: RunId) -> StatusView:
        """Record the question, then raise or answer."""
        self.asked.append(run_id)
        if self.error is not None:
            raise self.error
        return self.result


@dataclass
class FakeStop:
    """Answers `stop` with a receipt or raises."""

    disposition: StopDisposition = StopDisposition.EFFECTIVE
    error: Exception | None = None
    calls: list[tuple[RunId, str]] = field(default_factory=list)

    def stop(self, run_id: RunId, reason: str) -> StopReceipt:
        """Record the request, then raise or answer."""
        self.calls.append((run_id, reason))
        if self.error is not None:
            raise self.error
        return StopReceipt(run_id=run_id, disposition=self.disposition)


@dataclass
class FakeWait:
    """Answers `wait` from a script; the last step repeats."""

    steps: list[Step] = field(default_factory=lambda: [ended(TerminalReason.COMPLETED)])
    calls: list[tuple[RunId, float | None]] = field(default_factory=list)
    stop_probes: list[Callable[[], bool] | None] = field(default_factory=list)
    cost: Callable[[float | None], None] | None = None

    def wait(
        self,
        run_id: RunId,
        timeout_s: float | None,
        should_stop: Callable[[], bool] | None = None,
    ) -> WaitResult:
        """Record the call, spend the configured time, then play the next step."""
        self.calls.append((run_id, timeout_s))
        self.stop_probes.append(should_stop)
        if self.cost is not None:
            self.cost(timeout_s)
        step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        if isinstance(step, Exception):
            raise step
        if isinstance(step, WaitResult):
            return step
        return step()


@dataclass
class FakeReport:
    """Answers `report` with a result or raises."""

    result: ReportResult = field(
        default_factory=lambda: ReportResult(
            report_json=report_text(make_report(TerminalReason.COMPLETED)), pending=None
        )
    )
    error: Exception | None = None
    asked: list[RunId] = field(default_factory=list)

    def report(self, run_id: RunId) -> ReportResult:
        """Record the question, then raise or answer."""
        self.asked.append(run_id)
        if self.error is not None:
            raise self.error
        return self.result


@dataclass
class Book:
    """All fakes of one command run, the services built from them and the scope."""

    start: FakeStart = field(default_factory=FakeStart)
    status: FakeStatus = field(default_factory=FakeStatus)
    stop: FakeStop = field(default_factory=FakeStop)
    wait: FakeWait = field(default_factory=FakeWait)
    report: FakeReport = field(default_factory=FakeReport)
    scope: Scope = field(default_factory=Scope)
    factory_error: Exception | None = None
    factory_calls: int = 0

    def services(self) -> Services:
        """Build the services bundle; count the call and fail when the test says so."""
        self.factory_calls += 1
        if self.factory_error is not None:
            raise self.factory_error
        return Services(
            start=self.start,
            status=self.status,
            stop=self.stop,
            wait=self.wait,
            report=self.report,
        )


@dataclass(frozen=True)
class Outcome:
    """Everything one command run produced."""

    code: int
    stdout: str
    stderr: str


def invoke(argv: Sequence[str], book: Book) -> Outcome:
    """Run the command against the fakes of `book`."""
    stdout, stderr = io.StringIO(), io.StringIO()
    code = main(argv, stdout, stderr, FixedVersion(), book.services, book.scope)
    return Outcome(code, stdout.getvalue(), stderr.getvalue())
