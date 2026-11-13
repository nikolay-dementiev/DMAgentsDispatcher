"""Tests of the start-up sweep: runs left RUNNING by a dead supervisor are stopped and closed."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from agents_dispatcher.adapters.system.orphan_sweep import OrphanSweeper
from agents_dispatcher.adapters.system.process_table import LibprocProcessTable
from agents_dispatcher.adapters.system.run_owner_lock import FileRunOwners
from agents_dispatcher.adapters.system.sqlite_run_store import SqliteRunStore, StoredRun
from agents_dispatcher.application.errors import RunStoreError
from agents_dispatcher.application.ports import FinishRecord, ProcessIdentity, RunRecord
from agents_dispatcher.domain.run import RunId, RunState, TerminalReason
from tests.support.hard_timeout import HardTimeoutCase
from tests.support.narrow import present

SLEEPER = "import time; time.sleep(300)"
STUBBORN = (
    "import signal, sys, time;"
    " signal.signal(signal.SIGTERM, signal.SIG_IGN);"
    " print('ready', flush=True); time.sleep(300)"
)
LEADER_THAT_LEAVES = (
    "import subprocess, sys;"
    " subprocess.Popen([sys.executable, '-B', '-c', 'import time; time.sleep(300)'],"
    " stdout=subprocess.DEVNULL, stdin=subprocess.DEVNULL);"
    " print('ready', flush=True)"
)


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
        # A fresh id per test: markers of another test or suite must never match this one.
        self.run_id = RunId(str(uuid.uuid4()))
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.table = LibprocProcessTable()
        self.owners = FileRunOwners(str(Path(scratch.name) / "control"))
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
            if child.stdout is not None:
                child.stdout.close()

    def spawn(
        self, *, marker: bool, script: str = SLEEPER, announces: bool = False
    ) -> subprocess.Popen[bytes]:
        """Start a script in its own session, optionally carrying the run marker.

        With `announces` the script prints a line when it is ready, and this waits for it.
        """
        env = {**os.environ, **({"AGENTSD_RUN_ID": self.run_id.value} if marker else {})}
        child = subprocess.Popen(  # noqa: S603 - argv array of this interpreter, no shell
            [sys.executable, "-B", "-c", script],
            start_new_session=True,
            env=env,
            stdout=subprocess.PIPE if announces else None,
        )
        self.children.append(child)
        if announces and child.stdout is not None:
            child.stdout.readline()
        deadline = time.monotonic() + 10.0
        while self.table.start_time(child.pid) is None and time.monotonic() < deadline:
            time.sleep(0.05)
        return child

    def register(self, identity: ProcessIdentity | None) -> None:
        """Store a run as RUNNING with the given group identity."""
        self.store.begin(
            RunRecord(self.run_id, "/work/example", "b" * 40, self.run_id.branch_name, "2.1.304")
        )
        self.store.set_state(self.run_id, RunState.RUNNING)
        if identity is not None:
            self.store.set_process(self.run_id, identity)

    def sweeper(self) -> OrphanSweeper:
        """Build the sweeper under test."""
        return OrphanSweeper(self.store, self.table, self.owners, term_grace_s=0.3, kill_wait_s=1.0)

    def test_a_running_group_is_stopped_and_the_run_becomes_orphaned(self) -> None:
        """The group is found by its stored identity, stopped, and the run is closed."""
        child = self.spawn(marker=False)
        start = present(self.table.start_time(child.pid))
        self.register(ProcessIdentity(pgid=child.pid, start_time_s=start))
        report = self.sweeper().sweep()
        child.wait(timeout=10)
        self.assertFalse(alive(child.pid), msg="the group leader is gone")
        stored = self.store.get(self.run_id)
        self.assertEqual(stored.state, RunState.TERMINAL, msg="the run is closed")
        self.assertEqual(stored.terminal_reason, TerminalReason.ORPHANED, msg="as orphaned")
        self.assertEqual(report, [self.run_id], msg="the sweep reports the run it closed")
        stored_report = json.loads(stored.report_json or "{}")
        self.assertEqual(stored_report["terminal_reason"], "ORPHANED", msg="the report is stored")
        self.assertEqual(stored_report["state"], "TERMINAL", msg="the report is terminal")

    def test_a_process_with_a_different_start_time_is_left_alone(self) -> None:
        """A reused pid must never be killed: the stored start time has to match."""
        bystander = self.spawn(marker=False)
        wrong = present(self.table.start_time(bystander.pid)) - 3600.0
        self.register(ProcessIdentity(pgid=bystander.pid, start_time_s=wrong))
        self.sweeper().sweep()
        self.assertIsNone(bystander.poll(), msg="the bystander survives (not even a zombie)")
        self.assertEqual(
            self.store.get(self.run_id).terminal_reason,
            TerminalReason.ORPHANED,
            msg="run still closed",
        )

    def test_a_group_that_ignores_the_polite_stop_is_killed(self) -> None:
        """TERM is not enough for a stubborn worker: the sweep escalates to KILL."""
        stubborn = self.spawn(marker=False, script=STUBBORN, announces=True)
        start = present(self.table.start_time(stubborn.pid))
        self.register(ProcessIdentity(pgid=stubborn.pid, start_time_s=start))
        self.sweeper().sweep()
        stubborn.wait(timeout=10)
        self.assertFalse(alive(stubborn.pid), msg="the stubborn leader is gone")

    def test_a_group_whose_leader_is_gone_but_has_members_is_still_stopped(self) -> None:
        """The leader left; its group id is still held by a member of the run."""
        leader = self.spawn(marker=False, script=LEADER_THAT_LEAVES, announces=True)
        leader.wait(timeout=10)
        self.assertTrue(self.table.group_members(leader.pid), msg="a member holds the group")
        self.register(ProcessIdentity(pgid=leader.pid, start_time_s=1.0))
        self.sweeper().sweep()
        self.assertEqual(self.table.group_members(leader.pid), [], msg="the group is emptied")

    def test_a_process_that_cannot_be_signalled_does_not_stop_the_sweep(self) -> None:
        """EPERM on a marked pid (here launchd, pid 1) must not leave later runs unclosed."""

        class MarkedLaunchd(LibprocProcessTable):
            """A table that claims pid 1 carries this run's marker."""

            def __init__(self, marker: str) -> None:
                self.marker = marker

            def all_pids(self) -> list[int]:
                return [1]

            def environment(self, pid: int) -> dict[str, str] | None:  # noqa: ARG002 - fake
                return {"AGENTSD_RUN_ID": self.marker}

        self.register(None)
        sweeper = OrphanSweeper(
            self.store,
            MarkedLaunchd(self.run_id.value),
            self.owners,
            term_grace_s=0.3,
            kill_wait_s=1.0,
        )
        sweeper.sweep()
        self.assertEqual(self.store.get(self.run_id).state, RunState.TERMINAL, msg="still closed")

    def test_a_group_without_a_recorded_start_time_is_left_alone(self) -> None:
        """No start time means the identity cannot be checked, so the group is not signalled."""
        bystander = self.spawn(marker=False)
        self.register(ProcessIdentity(pgid=bystander.pid, start_time_s=None))
        self.sweeper().sweep()
        self.assertIsNone(bystander.poll(), msg="the unverifiable group survives")
        self.assertEqual(self.store.get(self.run_id).state, RunState.TERMINAL, msg="run closed")

    def test_the_system_groups_and_the_sweeping_process_own_group_are_never_signalled(self) -> None:
        """A damaged row must not make the sweep signal pgid 0, pgid 1 or its own group."""
        own = os.getpgrp()
        own_start = present(self.table.start_time(own))
        for pgid, start in ((0, own_start), (1, 1.0), (own, own_start)):
            with self.subTest(pgid=pgid), mock.patch("os.killpg") as killpg:
                scratch = tempfile.TemporaryDirectory()
                self.addCleanup(scratch.cleanup)
                self.store = SqliteRunStore(
                    str(Path(scratch.name) / "runs.sqlite3"), epoch_s=time.time, busy_s=2.0
                )
                self.register(ProcessIdentity(pgid=pgid, start_time_s=start))
                self.sweeper().sweep()
                self.assertEqual(killpg.call_args_list, [], msg=f"no signal to group {pgid}")

    def test_the_duration_never_goes_negative(self) -> None:
        """A wall clock that stepped back records zero, not a negative duration."""
        self.register(None)
        sweeper = OrphanSweeper(
            self.store,
            self.table,
            self.owners,
            term_grace_s=0.3,
            kill_wait_s=1.0,
            epoch_s=lambda: 0.0,
        )
        sweeper.sweep()
        self.assertEqual(self.store.get(self.run_id).duration_s, 0.0, msg="clamped at zero")

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
        self.assertEqual(self.store.get(self.run_id).state, RunState.TERMINAL, msg="closed")

    def test_finished_runs_are_not_touched(self) -> None:
        """Nothing to do when every run reached TERMINAL."""
        self.assertEqual(self.sweeper().sweep(), [], msg="empty sweep")

    def test_a_run_with_a_live_owner_is_left_alone(self) -> None:
        """A supervisor that still holds the run's lock is alive: nothing is stopped or closed."""
        child = self.spawn(marker=False)
        start = present(self.table.start_time(child.pid))
        self.register(ProcessIdentity(pgid=child.pid, start_time_s=start))
        ownership = self.owners.hold(self.run_id)
        self.addCleanup(ownership.release)
        self.assertEqual(self.sweeper().sweep(), [], msg="nothing closed")
        self.assertIsNone(child.poll(), msg="the worker group is untouched")
        self.assertEqual(self.store.get(self.run_id).state, RunState.RUNNING, msg="still running")

    def test_a_run_nobody_has_claimed_yet_is_not_an_orphan(self) -> None:
        """ACCEPTED means `start` still waits for its supervisor; the sweep must not close it."""
        self.store.begin(
            RunRecord(self.run_id, "/work/example", "b" * 40, self.run_id.branch_name, "2.1.304")
        )
        self.assertEqual(self.sweeper().sweep(), [], msg="nothing closed")
        self.assertEqual(self.store.get(self.run_id).state, RunState.ACCEPTED, msg="untouched")

    def test_one_named_run_can_be_closed_without_touching_the_others(self) -> None:
        """`wait` closes only its own target."""
        other = RunId(str(uuid.uuid4()))
        self.store.begin(RunRecord(other, "/work/example", "b" * 40, other.branch_name, "2.1.304"))
        self.store.set_state(other, RunState.RUNNING)
        self.register(None)
        sweeper = self.sweeper()
        self.assertTrue(sweeper.close_if_orphaned(self.run_id), msg="the named run is closed")
        self.assertEqual(self.store.get(other).state, RunState.RUNNING, msg="the other is not")
        self.assertFalse(sweeper.close_if_orphaned(self.run_id), msg="a finished run: nothing")
        self.assertFalse(sweeper.close_if_orphaned(RunId(str(uuid.uuid4()))), msg="unknown run")

    def test_losing_the_race_to_another_closer_is_not_an_error(self) -> None:
        """Two waiters may find the same dead owner; the second finds the run already closed."""
        self.register(None)
        real = self.store

        class Racing:
            """A store whose finish is beaten by someone else."""

            def unfinished(self) -> list[StoredRun]:
                return real.unfinished()

            def finish(self, run_id: RunId, record: FinishRecord) -> None:
                real.finish(run_id, record)
                message = "already finished"
                raise RunStoreError(message)

        sweeper = OrphanSweeper(
            Racing(),
            self.table,
            self.owners,
            term_grace_s=0.3,
            kill_wait_s=1.0,
        )
        self.assertFalse(sweeper.close_if_orphaned(self.run_id), msg="not counted as ours")


if __name__ == "__main__":
    unittest.main()
