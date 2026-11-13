"""Closes runs whose supervisor died (PLAN-4 section 2, PLAN-5 slice 3).

A run is orphaned when it was claimed by a supervisor (state past ACCEPTED), is not TERMINAL and
its owner lock is no longer held. Its worker group is identified by the stored
process-group id together with the leader's start time, so a reused pid is never signalled.
Processes that left the group but still carry the run marker are stopped too. The run then
becomes `ORPHANED` (ARCH L1432).
"""

import contextlib
import os
import signal
import time
from collections.abc import Callable
from typing import Protocol

from agents_dispatcher.adapters.system.json_report import report_text
from agents_dispatcher.adapters.system.seatbelt_confinement import MARKER_ENV, ProcessTable
from agents_dispatcher.adapters.system.sqlite_run_store import StoredRun
from agents_dispatcher.application.errors import RunStoreError
from agents_dispatcher.application.ports import FinishRecord, OwnerProbe
from agents_dispatcher.domain.run import RunId, RunReport, RunState, TerminalReason

START_TIME_TOLERANCE_S = 1.0
POLL_S = 0.05
SYSTEM_GROUP_MAX = 1  # pgid 0 means the caller's group and 1 is launchd: never a run's group


def fault_notes(run: StoredRun) -> tuple[str, ...]:
    """Return the note that names the supervisor's own account of its failure, if it gave one."""
    return (f"supervisor fault: {run.fault}",) if run.fault else ()


def orphan_report(run: StoredRun, duration_s: float) -> RunReport:
    """Build the report of a run whose supervisor died: what is known, and that it is unfinished."""
    return RunReport(
        run_id=run.run_id,
        state=RunState.TERMINAL,
        terminal_reason=TerminalReason.ORPHANED,
        repo=run.repo,
        base_commit=run.base_commit,
        branch=None,
        commits_on_branch=0,
        worker_cli_version=run.cli_version,
        worker_model=None,
        worker_exit_status=None,
        duration_s=duration_s,
        limits_configured=run.limits,
        limit_hit=None,
        survivors=(),
        result_text="",
        notes=(
            "the supervisor ended before the run did; the worker was stopped, nothing was sealed",
            *fault_notes(run),
        ),
    )


def unclaimed_report(run: StoredRun, duration_s: float) -> RunReport:
    """Build the report of a run that no supervisor ever claimed."""
    return RunReport(
        run_id=run.run_id,
        state=RunState.TERMINAL,
        terminal_reason=TerminalReason.LAUNCH_LOST,
        repo=run.repo,
        base_commit=None,
        branch=None,
        commits_on_branch=0,
        worker_cli_version=None,
        worker_model=None,
        worker_exit_status=None,
        duration_s=duration_s,
        limits_configured=run.limits,
        limit_hit=None,
        survivors=(),
        result_text="",
        notes=("the starter ended before a supervisor claimed the run", *fault_notes(run)),
    )


class UnfinishedRuns(Protocol):
    """The store operations the sweep uses."""

    def unfinished(self) -> list[StoredRun]:
        """Return the runs that are not TERMINAL."""
        ...

    def finish(self, run_id: RunId, record: FinishRecord) -> None:
        """Store the terminal outcome."""
        ...

    def finish_unclaimed(self, run_id: RunId, record: FinishRecord) -> bool:
        """Close a run nobody took over; False when it is no longer ACCEPTED."""
        ...


class OrphanSweeper:
    """Stops what an earlier, dead supervisor left behind and closes its runs."""

    def __init__(  # noqa: PLR0913 - the composition root wires exactly these collaborators
        self,
        store: UnfinishedRuns,
        table: ProcessTable,
        owners: OwnerProbe,
        *,
        term_grace_s: float,
        kill_wait_s: float,
        unclaimed_after_s: float,
        epoch_s: Callable[[], float] = time.time,
    ) -> None:
        """Keep the store, the process table, the owner probe, the timings and the clock.

        `unclaimed_after_s` is how old an ACCEPTED run must be before it counts as abandoned.
        """
        self._store = store
        self._table = table
        self._owners = owners
        self._term_grace_s = term_grace_s
        self._kill_wait_s = kill_wait_s
        self._unclaimed_after_s = unclaimed_after_s
        self._epoch_s = epoch_s

    def sweep(self) -> list[RunId]:
        """Stop and close every claimed, unfinished run that has no live owner."""
        return [run.run_id for run in self._store.unfinished() if self._close(run)]

    def close_if_orphaned(self, run_id: RunId) -> bool:
        """Close the named run when it is orphaned; False when it is not, or was closed already."""
        return any(self._close(run) for run in self._store.unfinished() if run.run_id == run_id)

    def _close(self, run: StoredRun) -> bool:
        """Stop what the run left behind and close it; False when it is not an orphan."""
        if run.state is RunState.ACCEPTED:
            return self._close_unclaimed(run)
        if self._owners.owner_alive(run.run_id):
            return False
        self._stop_group(run)
        self._stop_marked(run.run_id.value)
        duration = max(0.0, self._epoch_s() - run.started_at)
        record = FinishRecord(
            terminal_reason=TerminalReason.ORPHANED,
            branch=None,
            commits_on_branch=0,
            limit_hit=None,
            worker_exit_status=None,
            duration_s=duration,
            report_json=report_text(orphan_report(run, duration)),
        )
        try:
            self._store.finish(run.run_id, record)
        except RunStoreError:
            # Another process closed the run first; that is not a failure, but anything else is.
            if any(other.run_id == run.run_id for other in self._store.unfinished()):
                raise
            return False
        return True

    def _close_unclaimed(self, run: StoredRun) -> bool:
        """End an ACCEPTED run whose starter is gone; a supervisor that claims it first wins."""
        age = self._epoch_s() - run.started_at
        if age <= self._unclaimed_after_s or self._owners.owner_alive(run.run_id):
            return False  # a live `start` may still be waiting for its acknowledgement
        report = unclaimed_report(run, age)
        record = FinishRecord(
            terminal_reason=TerminalReason.LAUNCH_LOST,
            branch=None,
            commits_on_branch=0,
            limit_hit=None,
            worker_exit_status=None,
            duration_s=age,
            report_json=report_text(report),
        )
        return self._store.finish_unclaimed(run.run_id, record)

    def _stop_group(self, run: StoredRun) -> None:
        """TERM, wait, KILL the stored group, but only when it is still the same group."""
        if run.pgid is None or run.pg_start_time is None or not self._is_ours(run):
            return
        if run.pgid <= SYSTEM_GROUP_MAX or run.pgid == os.getpgrp():
            return  # a damaged row must never make us signal everything, launchd or ourselves
        self._signal_group(run.pgid, signal.SIGTERM)
        self._wait_until_empty(run.pgid, self._term_grace_s)
        if self._table.group_members(run.pgid):
            self._signal_group(run.pgid, signal.SIGKILL)
            self._wait_until_empty(run.pgid, self._kill_wait_s)

    def _is_ours(self, run: StoredRun) -> bool:
        """Return whether the stored group is the one the run started.

        A living leader must have the stored start time. A dead leader whose group still has
        members means the group id is still held by the run's own processes.
        """
        leader_start = self._table.start_time(run.pgid or 0)
        if leader_start is None:
            return bool(self._table.group_members(run.pgid or 0))
        return abs(leader_start - (run.pg_start_time or 0.0)) < START_TIME_TOLERANCE_S

    def _stop_marked(self, marker: str) -> None:
        """Kill processes outside the group that carry the run marker."""
        own = os.getpid()
        for pid in self._table.all_pids():
            environment = self._table.environment(pid)
            if pid != own and environment is not None and environment.get(MARKER_ENV) == marker:
                # It may have ended meanwhile; macOS answers EPERM for an exiting process.
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGKILL)

    @staticmethod
    def _signal_group(pgid: int, number: int) -> None:
        """Signal a process group; a group that is gone, or holds only zombies, is fine.

        macOS answers EPERM when every member is a zombie that its parent has not reaped yet.
        """
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, number)

    def _wait_until_empty(self, pgid: int, seconds: float) -> None:
        """Wait for the group to empty, at most `seconds`."""
        deadline = time.monotonic() + seconds
        while self._table.group_members(pgid) and time.monotonic() < deadline:
            time.sleep(POLL_S)
