"""The use cases a front end calls: one port per operation and the bundle of them."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from agents_dispatcher.application.run_queries import ReportResult, WaitResult
from agents_dispatcher.domain.routing import Recommendation
from agents_dispatcher.domain.run import RunId, TaskSpec
from agents_dispatcher.domain.views import RunSummary, StatusView, StopReceipt


class StartService(Protocol):
    """Accepts a run and hands it to a supervisor."""

    def start(
        self, spec: TaskSpec, should_stop: Callable[[], bool] | None = None
    ) -> RunSummary | Recommendation:
        """Start the run described by `spec`, or answer with a recommendation and no run.

        `should_stop` is asked once after the route is decided and before the run is committed;
        when it says yes the call raises `StartCancelledError` and creates nothing.
        """
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
