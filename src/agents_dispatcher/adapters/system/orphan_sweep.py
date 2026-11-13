"""The start-up sweep: closes runs that a dead supervisor left unfinished (PLAN-4 section 2).

A run is found by the store (state not TERMINAL). Its worker group is identified by the stored
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

from agents_dispatcher.adapters.system.seatbelt_confinement import MARKER_ENV, ProcessTable
from agents_dispatcher.adapters.system.sqlite_run_store import StoredRun
from agents_dispatcher.application.ports import FinishRecord
from agents_dispatcher.domain.run import RunId, TerminalReason

START_TIME_TOLERANCE_S = 1.0
POLL_S = 0.05


class UnfinishedRuns(Protocol):
    """The two store operations the sweep uses."""

    def unfinished(self) -> list[StoredRun]:
        """Return the runs that are not TERMINAL."""
        ...

    def finish(self, run_id: RunId, record: FinishRecord) -> None:
        """Store the terminal outcome."""
        ...


class OrphanSweeper:
    """Stops what an earlier, dead supervisor left behind and closes its runs."""

    def __init__(
        self,
        store: UnfinishedRuns,
        table: ProcessTable,
        *,
        term_grace_s: float,
        kill_wait_s: float,
        epoch_s: Callable[[], float] = time.time,
    ) -> None:
        """Keep the store, the process table, the stop timings and the wall clock."""
        self._store = store
        self._table = table
        self._term_grace_s = term_grace_s
        self._kill_wait_s = kill_wait_s
        self._epoch_s = epoch_s

    def sweep(self) -> list[RunId]:
        """Stop and close every unfinished run; return the ids that were closed."""
        closed = []
        for run in self._store.unfinished():
            self._stop_group(run)
            self._stop_marked(run.run_id.value)
            self._store.finish(
                run.run_id,
                FinishRecord(
                    terminal_reason=TerminalReason.ORPHANED,
                    branch=None,
                    commits_on_branch=0,
                    limit_hit=None,
                    worker_exit_status=None,
                    duration_s=max(0.0, self._epoch_s() - run.started_at),
                ),
            )
            closed.append(run.run_id)
        return closed

    def _stop_group(self, run: StoredRun) -> None:
        """TERM, wait, KILL the stored group, but only when it is still the same group."""
        if run.pgid is None or run.pg_start_time is None or not self._is_ours(run):
            return
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
                with contextlib.suppress(ProcessLookupError):  # it may have ended meanwhile
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
