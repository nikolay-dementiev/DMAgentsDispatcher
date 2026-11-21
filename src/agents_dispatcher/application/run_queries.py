"""The read-side use cases of a run: status, stop, wait and report."""

from collections.abc import Callable
from dataclasses import dataclass

from agents_dispatcher.application.errors import ReportUnavailableError, UnknownRunError
from agents_dispatcher.application.ports import (
    Clock,
    OrphanCloser,
    OwnerProbe,
    RunReader,
    RunSnapshot,
    Sleeper,
    StopRegistry,
)
from agents_dispatcher.domain.run import RunId, RunState
from agents_dispatcher.domain.views import (
    RunSummary,
    StatusView,
    StopReceipt,
    bounded_text,
    validate_reason,
)


@dataclass(frozen=True)
class WaitResult:
    """The outcome of a wait: ready (the run ended) or pending, with the last status."""

    ready: bool
    status: StatusView


@dataclass(frozen=True)
class ReportResult:
    """The stored report text of a finished run, or the status of an unfinished one."""

    report_json: str | None
    pending: StatusView | None


class RunStatus:
    """Shows a run; it only observes and never closes anything."""

    def __init__(self, *, reader: RunReader, owner: OwnerProbe, summary_bytes: int) -> None:
        """Keep the read ports and the bound of the task text."""
        self._reader = reader
        self._owner = owner
        self._summary_bytes = summary_bytes

    def status(self, run_id: RunId) -> StatusView:
        """Return the current view; raise `UnknownRunError` for an unknown run."""
        snapshot = self.snapshot(run_id)
        return self.view(snapshot)

    def snapshot(self, run_id: RunId) -> RunSnapshot:
        """Return the stored run; raise `UnknownRunError` for an unknown run."""
        snapshot = self._reader.find(run_id)
        if snapshot is None:
            message = f"no run with id {run_id.value}"
            raise UnknownRunError(message)
        return snapshot

    def view(self, snapshot: RunSnapshot) -> StatusView:
        """Build the public view of a stored run."""
        return StatusView(
            run=RunSummary(
                run_id=snapshot.run_id,
                task=bounded_text(snapshot.task, self._summary_bytes),
                state=snapshot.state,
                reason=snapshot.terminal_reason,
                branch=snapshot.branch,
                routing=snapshot.routing,
            ),
            revision=snapshot.revision,
            owner_alive=self._owner.owner_alive(snapshot.run_id),
            stop=snapshot.stop,
            quarantine=snapshot.quarantine,
        )


class StopRun:
    """Asks a run to stop."""

    def __init__(self, *, stops: StopRegistry, reason_bytes: int) -> None:
        """Keep the registry and the bound of the reason."""
        self._stops = stops
        self._reason_bytes = reason_bytes

    def stop(self, run_id: RunId, reason: str) -> StopReceipt:
        """Record a stop request; raise `ValueError` for a bad reason, `UnknownRunError`."""
        text = validate_reason(reason, self._reason_bytes)
        disposition = self._stops.request_stop(run_id, text)
        if disposition is None:
            message = f"no run with id {run_id.value}"
            raise UnknownRunError(message)
        return StopReceipt(run_id=run_id, disposition=disposition)


# A caller that was interrupted must be heard within about a second, whatever the scan interval.
MAX_PAUSE_S = 1.0


class WaitRun:
    """Waits for a run to end, closing it when its supervisor is gone."""

    def __init__(
        self,
        *,
        status: RunStatus,
        closer: OrphanCloser,
        clock: Clock,
        sleeper: Sleeper,
        scan_s: float,
    ) -> None:
        """Keep the collaborators and the poll interval."""
        self._status = status
        self._closer = closer
        self._clock = clock
        self._sleeper = sleeper
        self._scan_s = scan_s

    def wait(
        self,
        run_id: RunId,
        timeout_s: float | None,
        should_stop: Callable[[], bool] | None = None,
    ) -> WaitResult:
        """Wait up to `timeout_s` (forever for None); raise `UnknownRunError` for an unknown run.

        `should_stop` is asked between two looks; when it says yes the run's current status is
        returned as pending. The deadline is read from the clock, so the wait ends within one look
        of it however long each look took. No pause is longer than `MAX_PAUSE_S`, so `should_stop`
        is asked at least that often.
        """
        deadline = None if timeout_s is None else self._clock.now() + timeout_s
        while True:
            snapshot = self._status.snapshot(run_id)
            if snapshot.state is RunState.QUARANTINED:
                # Only an explicit resolution ends it, so waiting would only hang the caller.
                return WaitResult(ready=False, status=self._status.view(snapshot))
            if snapshot.state is not RunState.TERMINAL and self._closer.close_if_orphaned(run_id):
                snapshot = self._status.snapshot(run_id)
            if snapshot.state is RunState.TERMINAL:
                return WaitResult(ready=True, status=self._status.view(snapshot))
            if should_stop is not None and should_stop():
                return WaitResult(ready=False, status=self._status.view(snapshot))
            pause = min(self._scan_s, MAX_PAUSE_S)
            if deadline is not None:
                left = deadline - self._clock.now()
                if left <= 0:
                    return WaitResult(ready=False, status=self._status.view(snapshot))
                pause = min(pause, left)
            self._sleeper.sleep(pause)


class ReadReport:
    """Returns the stored report of a finished run."""

    def __init__(self, *, status: RunStatus) -> None:
        """Keep the status use case, which reads the stored run."""
        self._status = status

    def report(self, run_id: RunId) -> ReportResult:
        """Return the report text, or the status when the run is unfinished."""
        snapshot = self._status.snapshot(run_id)
        if snapshot.state is not RunState.TERMINAL:
            return ReportResult(report_json=None, pending=self._status.view(snapshot))
        if snapshot.report_json is None:
            message = f"run {run_id.value} ended but no report was stored for it"
            raise ReportUnavailableError(message)
        return ReportResult(report_json=snapshot.report_json, pending=None)
