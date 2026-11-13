"""Tests of the start-up sweep: runs left RUNNING by a dead supervisor are stopped and closed."""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from agents_dispatcher.adapters.system.orphan_sweep import OrphanSweeper

from agents_dispatcher.adapters.system.process_table import LibprocProcessTable
from agents_dispatcher.adapters.system.sqlite_run_store import SqliteRunStore
from agents_dispatcher.application.ports import ProcessIdentity, RunRecord
from agents_dispatcher.domain.run import RunId, RunState, TerminalReason
from tests.support.hard_timeout import HardTimeoutCase
from tests.support.narrow import present

RUN_ID = RunId("5a6b7c8d-9e0f-4a1b-8c2d-3e4f5a6b7c8d")
SLEEPER = "import time; time.sleep(300)"


def alive(pid: int) -> bool:
    """Return whether a process with this pid exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class OrphanSweepTests(HardTimeoutCase):
    """What the next start does about a run whose supervisor was killed."""

    def setUp(self) -> None:
        """Open a store in a temporary directory and keep a table."""
        super().setUp()
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.table = LibprocProcessTable()
        self.store = SqliteRunStore(
            str(Path(scratch.name) / "runs.sqlite3"), epoch_s=time.time, busy_s=2.0
        )
        self.children: list[subprocess.Popen[bytes]] = []
        self.addCleanup(self.reap)

    def reap(self) -> None:
        """Kill and reap every test child."""
        for child in self.children:
            child.kill()
            child.wait(timeout=10)

    def spawn(self, *, marker: bool) -> subprocess.Popen[bytes]:
        """Start a sleeper in its own session, optionally carrying the run marker."""
        env = {**os.environ, **({"AGENTSD_RUN_ID": RUN_ID.value} if marker else {})}
        child = subprocess.Popen(  # noqa: S603 - argv array of this interpreter, no shell
            [sys.executable, "-B", "-c", SLEEPER], start_new_session=True, env=env
        )
        self.children.append(child)
        deadline = time.monotonic() + 10.0
        while self.table.start_time(child.pid) is None and time.monotonic() < deadline:
            time.sleep(0.05)
        return child

    def register(self, identity: ProcessIdentity | None) -> None:
        """Store a run as RUNNING with the given group identity."""
        self.store.begin(
            RunRecord(RUN_ID, "/work/example", "b" * 40, RUN_ID.branch_name, "2.1.304")
        )
        self.store.set_state(RUN_ID, RunState.RUNNING)
        if identity is not None:
            self.store.set_process(RUN_ID, identity)

    def sweeper(self) -> OrphanSweeper:
        """Build the sweeper under test."""
        return OrphanSweeper(self.store, self.table, term_grace_s=0.3, kill_wait_s=1.0)

    def test_a_running_group_is_stopped_and_the_run_becomes_orphaned(self) -> None:
        """The group is found by its stored identity, stopped, and the run is closed."""
        child = self.spawn(marker=False)
        start = present(self.table.start_time(child.pid))
        self.register(ProcessIdentity(pgid=child.pid, start_time_s=start))
        report = self.sweeper().sweep()
        child.wait(timeout=10)
        self.assertFalse(alive(child.pid), msg="the group leader is gone")
        stored = self.store.get(RUN_ID)
        self.assertEqual(stored.state, RunState.TERMINAL, msg="the run is closed")
        self.assertEqual(stored.terminal_reason, TerminalReason.ORPHANED, msg="as orphaned")
        self.assertEqual(report, [RUN_ID], msg="the sweep reports the run it closed")

    def test_a_process_with_a_different_start_time_is_left_alone(self) -> None:
        """A reused pid must never be killed: the stored start time has to match."""
        bystander = self.spawn(marker=False)
        wrong = present(self.table.start_time(bystander.pid)) - 3600.0
        self.register(ProcessIdentity(pgid=bystander.pid, start_time_s=wrong))
        self.sweeper().sweep()
        self.assertTrue(alive(bystander.pid), msg="the bystander survives")
        self.assertEqual(
            self.store.get(RUN_ID).terminal_reason, TerminalReason.ORPHANED, msg="run still closed"
        )

    def test_a_marked_process_outside_the_group_is_stopped(self) -> None:
        """What left the group by setsid is found through the marker."""
        escaped = self.spawn(marker=True)
        self.register(ProcessIdentity(pgid=2**22, start_time_s=1.0))
        self.sweeper().sweep()
        escaped.wait(timeout=10)
        self.assertFalse(alive(escaped.pid), msg="the marked process was killed")

    def test_a_run_that_never_recorded_a_group_is_closed_too(self) -> None:
        """A supervisor killed before the worker started leaves nothing to stop."""
        self.register(None)
        self.sweeper().sweep()
        self.assertEqual(self.store.get(RUN_ID).state, RunState.TERMINAL, msg="closed")

    def test_finished_runs_are_not_touched(self) -> None:
        """Nothing to do when every run reached TERMINAL."""
        self.assertEqual(self.sweeper().sweep(), [], msg="empty sweep")


if __name__ == "__main__":
    unittest.main()
