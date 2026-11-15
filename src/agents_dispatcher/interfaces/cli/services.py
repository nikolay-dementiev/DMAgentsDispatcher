"""The ports the agentsd commands call: use cases, version and interrupt flag."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Protocol

from agents_dispatcher.application.run_queries import ReportResult, WaitResult
from agents_dispatcher.domain.routing import Recommendation
from agents_dispatcher.domain.run import RunId, TaskSpec
from agents_dispatcher.domain.views import RunSummary, StatusView, StopReceipt


class VersionSource(Protocol):
    """Reports the installed version of the distribution, if there is one."""

    def installed_version(self) -> str | None:
        """Return the installed version, or None when the metadata is missing."""
        ...


class StartService(Protocol):
    """Accepts a run and hands it to a supervisor."""

    def start(self, spec: TaskSpec) -> RunSummary | Recommendation:
        """Start the run described by `spec`, or answer with a recommendation and no run."""
        ...


class StatusService(Protocol):
    """Shows a run."""

    def status(self, run_id: RunId) -> StatusView:
        """Return the current view of the run."""
        ...


class StopService(Protocol):
    """Asks a run to stop."""

    def stop(self, run_id: RunId, reason: str) -> StopReceipt:
        """Record a stop request."""
        ...


class WaitService(Protocol):
    """Waits for a run to end."""

    def wait(
        self,
        run_id: RunId,
        timeout_s: float | None,
        should_stop: Callable[[], bool] | None = None,
    ) -> WaitResult:
        """Wait up to `timeout_s` seconds (forever for None), or until `should_stop` says so."""
        ...


class ReportService(Protocol):
    """Returns the stored report of a finished run."""

    def report(self, run_id: RunId) -> ReportResult:
        """Return the report text, or the status of an unfinished run."""
        ...


@dataclass(frozen=True)
class Services:
    """The use cases the commands call."""

    start: StartService
    status: StatusService
    stop: StopService
    wait: WaitService
    report: ReportService


ServiceFactory = Callable[[], Services]


class InterruptFlag(Protocol):
    """Tells whether the user interrupted the command."""

    def raised(self) -> bool:
        """Return True once SIGINT or SIGTERM arrived."""
        ...

    def count(self) -> int:
        """Return how many of these signals arrived in this scope."""
        ...


InterruptScope = Callable[[], AbstractContextManager[InterruptFlag]]
