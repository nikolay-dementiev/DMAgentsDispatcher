"""Resolves a quarantined run: look again, settle it, or let the owner release it.

A run is quarantined when a process of it could not be ended or shown gone. Nothing ends it by
itself: a fresh look that finds the run empty settles it with the outcome it would have had; a
live process leaves it held. The owner can release it instead, which frees the provider slot but
proves nothing about the processes, so the sandbox stays until a later look shows them gone.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import Enum

from agents_dispatcher.application.errors import (
    QuarantineStateError,
    ReleaseNotConfirmedError,
    UnknownRunError,
)
from agents_dispatcher.application.ports import (
    AbsenceLook,
    FinishRecord,
    HeldRun,
    KeptWorkspace,
    QuarantineEvents,
    QuarantineLedger,
    ReportAmender,
    SealedResult,
)
from agents_dispatcher.domain.routing import TaskMode
from agents_dispatcher.domain.run import RunId, RunState, Survivor, TerminalReason

RELEASE_NOTE = "released by operator"


class ResolutionOutcome(Enum):
    """What a resolution did."""

    SETTLED = "SETTLED"
    STILL_QUARANTINED = "STILL_QUARANTINED"
    RELEASED = "RELEASED"
    VERIFIED = "VERIFIED"
    STILL_UNVERIFIED = "STILL_UNVERIFIED"


@dataclass(frozen=True)
class Resolution:
    """The result of one resolution; `warnings` are things that went wrong after it took effect."""

    run_id: RunId
    outcome: ResolutionOutcome
    survivors: tuple[Survivor, ...] = ()
    warnings: tuple[str, ...] = ()


class ResolveQuarantine:
    """The quarantine commands and the re-check that `start` runs."""

    def __init__(  # noqa: PLR0913 - the composition root wires exactly these collaborators
        self,
        *,
        ledger: QuarantineLedger,
        look: AbsenceLook,
        workspace: KeptWorkspace,
        amender: ReportAmender,
        events: QuarantineEvents,
        epoch_s: Callable[[], float],
        stamp: Callable[[float], str],
    ) -> None:
        """Keep the collaborators; `stamp` writes an epoch time as the text a note carries."""
        self._ledger = ledger
        self._look = look
        self._workspace = workspace
        self._amender = amender
        self._events = events
        self._epoch_s = epoch_s
        self._stamp = stamp

    def resolve(self, run_id: RunId) -> Resolution:
        """Look again at a quarantined run, or at a released run whose absence is unverified."""
        run = self._require(run_id)
        if run.state is RunState.QUARANTINED:
            return self._settle(run)
        if run.state is RunState.TERMINAL and _awaits_verification(run):
            return self._verify(run)
        message = f"run {run_id.value} is {run.state.name}: there is nothing to resolve"
        raise QuarantineStateError(message)

    def release(self, run_id: RunId, *, confirm: str) -> Resolution:
        """End a quarantined run on the owner's word; it neither proves absence nor completion."""
        if confirm != run_id.value:
            message = f"the confirmation must repeat the run id {run_id.value} exactly"
            raise ReleaseNotConfirmedError(message)
        run = self._require(run_id)
        if run.state is not RunState.QUARANTINED:
            message = f"run {run_id.value} is {run.state.name}: only a quarantined run is released"
            raise QuarantineStateError(message)
        kept = self._kept(run_id)
        now = self._epoch_s()
        record = replace(
            kept,
            terminal_reason=TerminalReason.FAILED,
            report_json=self._amender.amend(
                kept.report_json, branch=None, commits=0, notes=(RELEASE_NOTE,), resolved=False
            ),
        )
        self._ledger.release(run_id, record, note=RELEASE_NOTE, released_at=now)
        self._events.record(f"{self._stamp(now)} QUARANTINE_RELEASED run={run_id.value}")
        return Resolution(run_id, ResolutionOutcome.RELEASED)

    def recheck_all(self) -> list[Resolution]:
        """Look at every run that waits for a look; a look that fails is logged and skipped."""
        results: list[Resolution] = []
        for run_id in self._ledger.awaiting():
            try:
                results.append(self.resolve(run_id))
            except (OSError, QuarantineStateError, UnknownRunError) as error:
                self._events.record(
                    f"{self._stamp(self._epoch_s())} QUARANTINE_RECHECK_FAILED "
                    f"run={run_id.value} error={error}"
                )
        return results

    def _require(self, run_id: RunId) -> HeldRun:
        """Return the run, or raise `UnknownRunError`."""
        run = self._ledger.held(run_id)
        if run is None:
            message = f"no run with id {run_id.value}"
            raise UnknownRunError(message)
        return run

    def _kept(self, run_id: RunId) -> FinishRecord:
        """Return the outcome the run would have ended as; a quarantined run always has one."""
        kept = self._ledger.pending(run_id)
        if kept is None:
            message = f"run {run_id.value} is quarantined but no outcome was kept for it"
            raise QuarantineStateError(message)
        return kept

    def _settle(self, run: HeldRun) -> Resolution:
        """End the run with the outcome it would have had when a fresh look finds it empty."""
        found = self._look.look(run.run_id)
        if not found.empty:
            return Resolution(run.run_id, ResolutionOutcome.STILL_QUARANTINED, found.survivors)
        kept = self._kept(run.run_id)
        sealed = (
            self._workspace.seal(run)
            if run.mode is TaskMode.WRITE
            else SealedResult(branch=None, commits_on_branch=0)
        )
        now = self._epoch_s()
        note = (
            f"quarantined, absence verified at {self._stamp(now)}, "
            "files may have changed after the worker's exit"
        )
        record = replace(
            kept,
            branch=sealed.branch,
            commits_on_branch=sealed.commits_on_branch,
            report_json=self._amender.amend(
                kept.report_json,
                branch=sealed.branch,
                commits=sealed.commits_on_branch,
                notes=(note,),
                resolved=True,
            ),
        )
        self._ledger.settle(run.run_id, record, verified_at=now)
        return Resolution(run.run_id, ResolutionOutcome.SETTLED, warnings=self._remove(run))

    def _verify(self, run: HeldRun) -> Resolution:
        """Remove the sandbox of a released run once a fresh look finds it empty."""
        found = self._look.look(run.run_id)
        if not found.empty:
            return Resolution(run.run_id, ResolutionOutcome.STILL_UNVERIFIED, found.survivors)
        warnings = self._remove(run)
        self._ledger.mark_verified(run.run_id, verified_at=self._epoch_s())
        return Resolution(run.run_id, ResolutionOutcome.VERIFIED, warnings=warnings)

    def _remove(self, run: HeldRun) -> tuple[str, ...]:
        """Remove the sandbox; the run is already over, so a failure is only reported."""
        try:
            self._workspace.remove(run)
        except OSError as error:
            return (f"the sandbox of run {run.run_id.value} could not be removed: {error}",)
        return ()


def _awaits_verification(run: HeldRun) -> bool:
    """Return whether a terminal run was released and its absence is still unverified."""
    return run.released_at is not None and run.absence_verified_at is None
