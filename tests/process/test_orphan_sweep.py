"""Tests of the start-up sweep: runs left RUNNING by a dead supervisor are stopped and closed."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from collections.abc import Callable, Sequence
from pathlib import Path
from unittest import mock

from agents_dispatcher.adapters.system.orphan_sweep import OrphanSweeper
from agents_dispatcher.adapters.system.os_signaller import OsSignaller
from agents_dispatcher.adapters.system.process_table import LibprocProcessTable
from agents_dispatcher.adapters.system.quarantine_look import QuarantineLook
from agents_dispatcher.adapters.system.run_owner_lock import FileRunOwners
from agents_dispatcher.adapters.system.sqlite_run_store import SqliteRunStore, StoredRun
from agents_dispatcher.application.containment import CleanupResult, CleanupVerdict
from agents_dispatcher.application.errors import RunStoreError
from agents_dispatcher.application.ports import (
    AcceptedRun,
    FinishRecord,
    ProcessIdentity,
    SignalResult,
)
from agents_dispatcher.domain.attribution import Attributed, RunFacts, Source
from agents_dispatcher.domain.process import Absence, ProcessBasics, ProcessKey
from agents_dispatcher.domain.run import Limits, RunId, RunState, Survivor, TerminalReason
from tests.support.confinement_kit import nobody_protected, real_cleanup
from tests.support.hard_timeout import HardTimeoutCase
from tests.support.narrow import present
from tests.support.routing import ROUTING
from tests.support.start_times import start_time_of

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


def accepted(run_id: RunId) -> AcceptedRun:
    """Return an accepted run for the store."""
    return AcceptedRun(run_id, "/work/example", "fix the bug", Limits(60.0, 1024), ROUTING)


def alive(pid: int) -> bool:
    """Return whether a process with this pid exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class RecordingResiduals:
    """The SDK log of the sweep; it remembers what it was told."""

    def __init__(self) -> None:
        """Start with nothing written."""
        self.held_calls: list[tuple[RunId, tuple[str, ...]]] = []
        self.unseen_calls: list[RunId] = []

    def unreadable(self, run_id: RunId, names: tuple[str, ...]) -> None:
        """Ignore processes that could not be read; these tests do not look at them."""

    def held(self, run_id: RunId, names: tuple[str, ...]) -> None:
        """Keep the names of the processes the sweep spared and held the run for."""
        self.held_calls.append((run_id, names))

    def unseen(self, run_id: RunId) -> None:
        """Keep the run whose sign of life could not be found."""
        self.unseen_calls.append(run_id)


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
        self.run_area = Path(scratch.name).resolve() / "runs"
        self.control = Path(scratch.name) / "control"
        self.owners = FileRunOwners(str(self.control))
        self.residuals = RecordingResiduals()
        self.store = SqliteRunStore(
            str(Path(scratch.name) / "runs.sqlite3"), epoch_s=time.time, busy_s=2.0
        )
        self.children: list[subprocess.Popen[bytes]] = []
        self.notices: list[str] = []
        self.addCleanup(self.reap)

    def reap(self) -> None:
        """Kill and reap every test child."""
        for child in self.children:
            child.kill()
            child.wait(timeout=10)
            if child.stdout is not None:
                child.stdout.close()

    def spawn(
        self,
        *,
        marker: bool,
        script: str = SLEEPER,
        announces: bool = False,
        cwd: Path | None = None,
    ) -> subprocess.Popen[bytes]:
        """Start a script in its own session, optionally carrying the run marker.

        With `announces` the script prints a line when it is ready, and this waits for it.
        """
        env = {**os.environ, **({"AGENTSD_RUN_ID": self.run_id.value} if marker else {})}
        child = subprocess.Popen(  # noqa: S603 - argv array of this interpreter, no shell
            [sys.executable, "-B", "-c", script],
            start_new_session=True,
            env=env,
            cwd=cwd,
            stdout=subprocess.PIPE if announces else None,
        )
        self.children.append(child)
        if announces and child.stdout is not None:
            child.stdout.readline()
        deadline = time.monotonic() + 10.0
        while start_time_of(self.table, child.pid) is None and time.monotonic() < deadline:
            time.sleep(0.05)
        return child

    def register(self, identity: ProcessIdentity | None) -> None:
        """Store a run as RUNNING with the given group identity."""
        self.store.accept(accepted(self.run_id))
        self.store.set_state(self.run_id, RunState.RUNNING)
        if identity is not None:
            self.store.set_process(self.run_id, identity)

    def sweeper(self) -> OrphanSweeper:
        """Build the sweeper under test."""
        return self.sweeper_at(time.time)

    def sweeper_at(self, epoch_s: Callable[[], float]) -> OrphanSweeper:
        """Build the sweeper under test with this wall clock and a 35 s unclaimed bound."""
        return OrphanSweeper(
            self.store,
            self.table,
            self.owners,
            term_grace_s=0.3,
            kill_wait_s=1.0,
            unclaimed_after_s=35.0,
            run_area=str(self.run_area),
            containment=real_cleanup(self.table).run,
            protected=nobody_protected,
            epoch_s=epoch_s,
            notify=self.notices.append,
            residuals=self.residuals,
        )

    def sweeper_using(self, containment: Callable[[RunFacts], CleanupResult]) -> OrphanSweeper:
        """Build the sweeper under test over this cleanup instead of the real one."""
        return OrphanSweeper(
            self.store,
            self.table,
            self.owners,
            term_grace_s=0.3,
            kill_wait_s=1.0,
            unclaimed_after_s=35.0,
            run_area=str(self.run_area),
            containment=containment,
            protected=nobody_protected,
            notify=self.notices.append,
            residuals=self.residuals,
        )

    def test_a_dead_run_with_a_process_that_cannot_be_ended_is_quarantined_not_orphaned(
        self,
    ) -> None:
        """The cleanup could not show the run empty: it is held back, not declared over."""
        self.register(None)
        stubborn = Attributed(ProcessKey(4242, 777.0), frozenset({Source.MARKER}))

        def uncertain(facts: RunFacts) -> CleanupResult:  # noqa: ARG001 - the facts are not read
            return CleanupResult(
                CleanupVerdict.CLEANUP_UNCERTAIN, 8, (), (stubborn,), (), frozenset()
            )

        self.assertTrue(
            self.sweeper_using(uncertain).close_if_orphaned(self.run_id),
            msg="the run was dealt with",
        )
        stored = self.store.get(self.run_id)
        self.assertEqual(stored.state, RunState.QUARANTINED, msg="held back")
        self.assertIsNone(stored.terminal_reason, msg="with no end yet")
        self.assertEqual(
            self.store.survivors(self.run_id),
            [Survivor(4242, 777.0, ("marker",))],
            msg="the survivor is recorded",
        )
        kept = self.store.pending(self.run_id)
        self.assertIsNotNone(kept, msg="the outcome it would have ended with is kept")
        self.assertEqual(
            present(kept).terminal_reason, TerminalReason.ORPHANED, msg="orphaned, once released"
        )

    def test_a_dead_run_whose_table_could_not_be_shown_empty_is_quarantined_without_survivors(
        self,
    ) -> None:
        """An uncertain verdict with nothing seen is still not a closed run."""
        self.register(None)

        def uncertain(facts: RunFacts) -> CleanupResult:  # noqa: ARG001 - the facts are not read
            return CleanupResult(CleanupVerdict.CLEANUP_UNCERTAIN, 8, (), (), (), frozenset())

        self.sweeper_using(uncertain).close_if_orphaned(self.run_id)
        stored = self.store.get(self.run_id)
        self.assertEqual(stored.state, RunState.QUARANTINED, msg="held back")
        self.assertEqual(self.store.survivors(self.run_id), [], msg="with no survivors recorded")

    def test_a_quarantined_run_is_not_an_orphan_when_its_supervisor_is_gone(self) -> None:
        """Its supervisor has ended on purpose; the sweep must not close it over the survivors."""
        self.register(None)
        self.store.quarantine(
            self.run_id,
            FinishRecord(TerminalReason.COMPLETED, None, 0, None, 0, 1.0, "{}"),
            [Survivor(4242, 777.0, ("marker",))],
        )
        sweeper = self.sweeper()
        self.assertEqual(sweeper.sweep(), [], msg="nothing was closed")
        self.assertFalse(sweeper.close_if_orphaned(self.run_id), msg="and not by name either")
        self.assertEqual(self.store.get(self.run_id).state, RunState.QUARANTINED, msg="still held")

    def test_an_accepted_run_nobody_claimed_in_time_is_closed_as_launch_lost(self) -> None:
        """The starter died between accept and launch: `wait` must not hang on it."""
        self.store.accept(accepted(self.run_id))
        later = self.sweeper_at(lambda: time.time() + 60.0)
        self.assertTrue(later.close_if_orphaned(self.run_id), msg="the stale run is closed")
        stored = self.store.get(self.run_id)
        self.assertEqual(stored.terminal_reason, TerminalReason.LAUNCH_LOST, msg="LAUNCH_LOST")
        self.assertIn("starter ended", str(stored.report_json), msg="the note names the cause")

    def test_a_run_that_cannot_be_closed_does_not_stop_the_sweep_of_the_others(self) -> None:
        """`start` sweeps before it accepts: one broken stale run must not block a new run."""
        broken, healthy = RunId(str(uuid.uuid4())), RunId(str(uuid.uuid4()))
        self.store.accept(accepted(broken))
        self.store.accept(accepted(healthy))
        real = self.store

        class BreaksOne:
            """A store whose close of one run fails with a database error."""

            def unfinished(self) -> list[StoredRun]:
                return real.unfinished()

            def finish(self, run_id: RunId, record: FinishRecord) -> None:
                real.finish(run_id, record)

            def finish_unclaimed(self, run_id: RunId, record: FinishRecord) -> bool:
                if run_id == broken:
                    message = "disk is full"
                    raise RunStoreError(message)
                return real.finish_unclaimed(run_id, record)

            def quarantine(
                self, run_id: RunId, record: FinishRecord, survivors: Sequence[Survivor]
            ) -> None:
                real.quarantine(run_id, record, survivors)

        sweeper = OrphanSweeper(
            BreaksOne(),
            self.table,
            self.owners,
            term_grace_s=0.3,
            kill_wait_s=1.0,
            unclaimed_after_s=35.0,
            run_area=str(self.run_area),
            containment=real_cleanup(self.table).run,
            protected=nobody_protected,
            epoch_s=lambda: time.time() + 60.0,
            notify=self.notices.append,
            residuals=self.residuals,
        )
        closed = sweeper.sweep()
        self.assertEqual(closed, [healthy], msg="the healthy stale run is still closed")
        self.assertEqual(len(self.notices), 1, msg="one notice for the one failure")
        self.assertIn(broken.value, self.notices[0], msg="the broken run is named")
        self.assertIn("disk is full", self.notices[0], msg="and the error")
        with self.assertRaises(RunStoreError, msg="a wait on that run still reports it"):
            sweeper.close_if_orphaned(broken)

    def test_a_recorded_fault_reaches_the_notes_of_a_launch_lost_report(self) -> None:
        """The caller learns why the supervisor gave up, not only that it did."""
        self.store.accept(accepted(self.run_id))
        self.store.record_fault(self.run_id, "settings: no claude executable found")
        later = self.sweeper_at(lambda: time.time() + 60.0)
        later.close_if_orphaned(self.run_id)
        notes = json.loads(str(self.store.get(self.run_id).report_json))["notes"]
        self.assertIn(
            "supervisor fault: settings: no claude executable found", notes, msg="cause in notes"
        )

    def test_a_recorded_fault_reaches_the_notes_of_an_orphaned_report(self) -> None:
        """A supervisor that failed after the claim leaves its cause in the closed run."""
        self.register(None)
        self.store.record_fault(self.run_id, "OSError: disk is full")
        self.sweeper().sweep()
        notes = json.loads(str(self.store.get(self.run_id).report_json))["notes"]
        self.assertIn("supervisor fault: OSError: disk is full", notes, msg="cause in notes")

    def test_an_accepted_run_younger_than_the_bound_is_left_alone(self) -> None:
        """A live `start` is still waiting for its acknowledgement."""
        self.store.accept(accepted(self.run_id))
        sooner = self.sweeper_at(lambda: time.time() + 5.0)
        self.assertFalse(sooner.close_if_orphaned(self.run_id), msg="too young to close")
        self.assertEqual(self.store.get(self.run_id).state, RunState.ACCEPTED, msg="untouched")

    def test_an_accepted_run_with_a_live_owner_is_left_alone(self) -> None:
        """A supervisor that holds the lock but has not claimed yet still wins."""
        self.store.accept(accepted(self.run_id))
        ownership = self.owners.hold(self.run_id)
        self.addCleanup(ownership.release)
        later = self.sweeper_at(lambda: time.time() + 60.0)
        self.assertFalse(later.close_if_orphaned(self.run_id), msg="owner is alive")

    def test_a_running_group_is_stopped_and_the_run_becomes_orphaned(self) -> None:
        """The group is found by its stored identity, stopped, and the run is closed."""
        child = self.spawn(marker=False)
        start = present(start_time_of(self.table, child.pid))
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
        wrong = present(start_time_of(self.table, bystander.pid)) - 3600.0
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
        start = present(start_time_of(self.table, stubborn.pid))
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

    def test_a_process_that_cannot_be_signalled_quarantines_the_run_and_the_sweep_goes_on(
        self,
    ) -> None:
        """A refused signal holds that run back; it must not leave later runs unclosed."""
        marked = self.spawn(marker=True)

        class Refusing(OsSignaller):
            """The real signaller, except that it refuses the one marked process."""

            def _refusal(self, key: ProcessKey) -> SignalResult | None:
                if key.pid == marked.pid:
                    return SignalResult.DENIED
                return super()._refusal(key)

        self.register(None)
        sweeper = OrphanSweeper(
            self.store,
            self.table,
            self.owners,
            term_grace_s=0.3,
            kill_wait_s=1.0,
            unclaimed_after_s=35.0,
            run_area=str(self.run_area),
            containment=real_cleanup(self.table, Refusing(self.table)).run,
            protected=nobody_protected,
            notify=self.notices.append,
            residuals=self.residuals,
        )
        sweeper.sweep()
        stored = self.store.get(self.run_id)
        self.assertEqual(stored.state, RunState.QUARANTINED, msg="held back, not declared over")
        self.assertIn(
            marked.pid,
            [item.pid for item in self.store.survivors(self.run_id)],
            msg="the survivor is recorded",
        )
        kept = present(self.store.pending(self.run_id))
        self.assertIn(f"pid {marked.pid}", kept.report_json, msg="and named in the kept report")

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
        own_start = present(start_time_of(self.table, own))
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
            unclaimed_after_s=35.0,
            run_area=str(self.run_area),
            containment=real_cleanup(self.table).run,
            protected=nobody_protected,
            epoch_s=lambda: 0.0,
            notify=self.notices.append,
            residuals=self.residuals,
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

    def run_folder(self) -> Path:
        """Create the run's sandbox folder the way the workspace does and return its worktree."""
        worktree = self.run_area / self.run_id.sandbox_name / "worktree"
        worktree.mkdir(parents=True)
        return worktree

    def test_a_process_that_started_in_the_run_folder_after_the_run_began_is_stopped(self) -> None:
        """No marker, no group: the folder it stayed in names the run (source d)."""
        worktree = self.run_folder()
        self.register(ProcessIdentity(pgid=2**22, start_time_s=1.0))
        escaped = self.spawn(marker=False, cwd=worktree)
        self.show_alive_until(time.time())
        self.sweeper().sweep()
        escaped.wait(timeout=10)
        self.assertFalse(alive(escaped.pid), msg="the process in the run folder was killed")

    def test_a_process_that_sat_in_the_run_folder_before_the_run_began_is_left_alone(self) -> None:
        """The guard: what was there before the run is not the run's, whatever its directory."""
        worktree = self.run_folder()
        bystander = self.spawn(marker=False, cwd=worktree)
        self.register(ProcessIdentity(pgid=2**22, start_time_s=1.0))
        self.sweeper().sweep()
        self.assertIsNone(bystander.poll(), msg="the older process was not touched")

    def show_alive_until(self, at: float) -> None:
        """Leave the sign of life a supervisor leaves: its owner lock last touched at `at`."""
        self.owners.hold(self.run_id).release()
        lock = self.control / "locks" / f"{self.run_id.value}.lock"
        os.utime(lock, (at, at))

    def test_a_process_that_entered_the_folder_after_the_last_sign_of_life_is_never_signalled(
        self,
    ) -> None:
        """Owner decision 2026-11-21: the folder rule stops at the run's last sign of life."""
        worktree = self.run_folder()
        self.register(ProcessIdentity(pgid=2**22, start_time_s=1.0))
        self.show_alive_until(time.time())
        time.sleep(0.3)
        visitor = self.spawn(marker=False, cwd=worktree)
        self.sweeper().sweep()
        self.assertIsNone(visitor.poll(), msg="the late visitor was not touched")
        self.assertIs(
            self.store.get(self.run_id).state, RunState.QUARANTINED, msg="and the run is held"
        )
        self.assertEqual(
            self.residuals.held_calls,
            [(self.run_id, (f"pid {visitor.pid}",))],
            msg="the visitor is named in the SDK log",
        )

    def test_a_resolve_of_a_swept_run_never_signals_the_visitor_and_settles_once_it_leaves(
        self,
    ) -> None:
        """The re-check uses the sweep's bound, not the later quarantine time."""
        worktree = self.run_folder()
        self.register(ProcessIdentity(pgid=2**22, start_time_s=1.0))
        self.show_alive_until(time.time())
        time.sleep(0.3)
        visitor = self.spawn(marker=False, cwd=worktree)
        self.sweeper().sweep()
        look = QuarantineLook(
            self.store,
            self.table,
            run_area=str(self.run_area),
            containment=real_cleanup(self.table).run,
            protected=nobody_protected,
        )
        held = look.look(self.run_id)
        self.assertIsNone(visitor.poll(), msg="the re-check did not signal the visitor")
        self.assertEqual(held.residual, (f"pid {visitor.pid}",), msg="it is named as the holder")
        visitor.kill()
        visitor.wait(timeout=10)
        free = look.look(self.run_id)
        self.assertEqual(
            (free.empty, free.residual), (True, ()), msg="once it left the folder is free"
        )

    def test_a_file_touched_in_the_worktree_after_the_death_does_not_move_the_bound(self) -> None:
        """Only what the run itself writes counts; a person's files do not."""
        worktree = self.run_folder()
        self.register(ProcessIdentity(pgid=2**22, start_time_s=1.0))
        self.show_alive_until(time.time())
        time.sleep(0.3)
        visitor = self.spawn(marker=False, cwd=worktree)
        (worktree / "notes.txt").write_text("looked at later\n", encoding="utf-8")
        self.sweeper().sweep()
        self.assertIsNone(visitor.poll(), msg="still not touched")

    def test_a_run_with_no_sign_of_life_attributes_nothing_by_folder(self) -> None:
        """No lock file: the bound is the start of the run, and the log says so."""
        worktree = self.run_folder()
        self.register(ProcessIdentity(pgid=2**22, start_time_s=1.0))
        visitor = self.spawn(marker=False, cwd=worktree)
        self.sweeper().sweep()
        self.assertIsNone(visitor.poll(), msg="nothing was attributed by the folder")
        self.assertEqual(self.residuals.unseen_calls, [self.run_id], msg="no sign of life, said")

    def test_a_live_group_makes_the_bound_the_start_of_the_sweep(self) -> None:
        """A worker that outlived its supervisor means the run is alive now."""
        worktree = self.run_folder()
        leader = self.spawn(marker=False)
        leader_start = present(start_time_of(self.table, leader.pid))
        self.register(ProcessIdentity(pgid=leader.pid, start_time_s=leader_start))
        self.show_alive_until(time.time())
        time.sleep(0.3)
        visitor = self.spawn(marker=False, cwd=worktree)
        self.sweeper().sweep()
        leader.wait(timeout=10)
        visitor.wait(timeout=10)
        self.assertFalse(alive(visitor.pid), msg="the folder rule reached up to the sweep start")
        self.assertIs(self.store.get(self.run_id).state, RunState.TERMINAL, msg="and the run ended")

    def test_a_group_whose_leader_belongs_to_someone_else_is_not_ours(self) -> None:
        """A leader that cannot be read is another user's process, not a dead leader of ours."""

        class ForeignLeader(LibprocProcessTable):
            """A table where the stored leader reads as another user's and its group is busy."""

            def basics(self, pid: int) -> ProcessBasics | Absence:  # noqa: ARG002 - fake
                return Absence.DENIED

            def group_members(self, pgid: int) -> list[int]:  # noqa: ARG002 - fake
                return [os.getpid()]

        self.register(ProcessIdentity(pgid=2**22, start_time_s=1.0))
        with mock.patch("os.killpg") as killpg:
            self.table = ForeignLeader()
            self.sweeper().sweep()
        self.assertEqual(killpg.call_args_list, [], msg="the foreign group was not signalled")

    def test_a_damaged_group_row_never_becomes_a_group_to_attribute(self) -> None:
        """Pgid 0, pgid 1 and our own group would claim every process of a whole group."""
        captured: list[RunFacts] = []

        def capture(facts: RunFacts) -> CleanupResult:
            captured.append(facts)
            return CleanupResult(CleanupVerdict.OBSERVED_EMPTY, 2, (), (), (), frozenset())

        own_start = present(start_time_of(self.table, os.getpgrp()))
        for pgid, start in ((0, own_start), (1, 1.0), (os.getpgrp(), own_start)):
            self.run_id = RunId(str(uuid.uuid4()))
            self.register(ProcessIdentity(pgid=pgid, start_time_s=start))
            sweeper = OrphanSweeper(
                self.store,
                self.table,
                self.owners,
                term_grace_s=0.3,
                kill_wait_s=1.0,
                unclaimed_after_s=35.0,
                run_area=str(self.run_area),
                containment=capture,
                protected=nobody_protected,
                notify=self.notices.append,
                residuals=self.residuals,
            )
            sweeper.close_if_orphaned(self.run_id)
        self.assertEqual([facts.group for facts in captured], [-1, -1, -1], msg="no group at all")

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
        start = present(start_time_of(self.table, child.pid))
        self.register(ProcessIdentity(pgid=child.pid, start_time_s=start))
        ownership = self.owners.hold(self.run_id)
        self.addCleanup(ownership.release)
        self.assertEqual(self.sweeper().sweep(), [], msg="nothing closed")
        self.assertIsNone(child.poll(), msg="the worker group is untouched")
        self.assertEqual(self.store.get(self.run_id).state, RunState.RUNNING, msg="still running")

    def test_a_run_nobody_has_claimed_yet_is_not_an_orphan(self) -> None:
        """ACCEPTED means `start` still waits for its supervisor; the sweep must not close it."""
        self.store.accept(accepted(self.run_id))
        self.assertEqual(self.sweeper().sweep(), [], msg="nothing closed")
        self.assertEqual(self.store.get(self.run_id).state, RunState.ACCEPTED, msg="untouched")

    def test_one_named_run_can_be_closed_without_touching_the_others(self) -> None:
        """`wait` closes only its own target."""
        other = RunId(str(uuid.uuid4()))
        self.store.accept(accepted(other))
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

            def finish_unclaimed(self, run_id: RunId, record: FinishRecord) -> bool:
                return real.finish_unclaimed(run_id, record)

            def quarantine(
                self, run_id: RunId, record: FinishRecord, survivors: Sequence[Survivor]
            ) -> None:
                real.quarantine(run_id, record, survivors)

        sweeper = OrphanSweeper(
            Racing(),
            self.table,
            self.owners,
            term_grace_s=0.3,
            kill_wait_s=1.0,
            unclaimed_after_s=35.0,
            run_area=str(self.run_area),
            containment=real_cleanup(self.table).run,
            protected=nobody_protected,
            notify=self.notices.append,
            residuals=self.residuals,
        )
        self.assertFalse(sweeper.close_if_orphaned(self.run_id), msg="not counted as ours")

    def test_a_store_failure_that_leaves_the_run_open_is_not_hidden(self) -> None:
        """If the run is still unfinished, the failure is real and reaches the caller."""
        self.register(None)
        real = self.store

        class Failing:
            """A store whose finish fails without closing anything."""

            def unfinished(self) -> list[StoredRun]:
                return real.unfinished()

            def finish(self, run_id: RunId, record: FinishRecord) -> None:  # noqa: ARG002
                message = "disk is full"
                raise RunStoreError(message)

            def finish_unclaimed(self, run_id: RunId, record: FinishRecord) -> bool:
                return real.finish_unclaimed(run_id, record)

            def quarantine(
                self, run_id: RunId, record: FinishRecord, survivors: Sequence[Survivor]
            ) -> None:
                real.quarantine(run_id, record, survivors)

        sweeper = OrphanSweeper(
            Failing(),
            self.table,
            self.owners,
            term_grace_s=0.3,
            kill_wait_s=1.0,
            unclaimed_after_s=35.0,
            run_area=str(self.run_area),
            containment=real_cleanup(self.table).run,
            protected=nobody_protected,
            notify=self.notices.append,
            residuals=self.residuals,
        )
        with self.assertRaises(RunStoreError, msg="the failure is raised"):
            sweeper.close_if_orphaned(self.run_id)


if __name__ == "__main__":
    unittest.main()
