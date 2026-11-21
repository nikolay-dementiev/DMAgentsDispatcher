"""Tests of the names of other runs' processes that a cleanup must never signal."""

import tempfile
import unittest
import uuid
from pathlib import Path

from agents_dispatcher.adapters.system.process_table import LibprocProcessTable
from agents_dispatcher.adapters.system.quarantine_look import QuarantineLook
from agents_dispatcher.adapters.system.sqlite_run_store import SqliteRunStore
from agents_dispatcher.application.containment import ContainmentCleanup
from agents_dispatcher.application.ports import AcceptedRun, FinishRecord, ProcessIdentity
from agents_dispatcher.application.settings import ObservationSettings
from agents_dispatcher.composition.wiring import live_run_identities
from agents_dispatcher.domain.process import ProcessKey
from agents_dispatcher.domain.run import Limits, RunId, RunState, TerminalReason
from tests.support.clock import FakeClock
from tests.support.fake_world import FakeProcess, FakeWorld
from tests.support.routing import ROUTING
from tests.support.run_book import FakeSleeper

NOW = 2000.5  # when the run is quarantined
ACCEPTED = 1000.0
OTHER_SUPERVISOR = ProcessIdentity(6001, 1500.0)  # started between the accept and the quarantine
OTHER_LEADER = ProcessIdentity(6002, 1501.0)
KEPT = FinishRecord(TerminalReason.COMPLETED, None, 0, None, 0, 1.0, "{}")


def new_run_id() -> RunId:
    """Return a fresh run id."""
    return RunId(str(uuid.uuid4()))


class IdentityCase(unittest.TestCase):
    """A store with two unfinished runs, each with a supervisor and a shim leader."""

    def setUp(self) -> None:
        """Accept both runs and give each its processes."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.run_area = Path(directory.name).resolve() / "runs"
        self.clock = ACCEPTED
        self.store = SqliteRunStore(
            str(Path(directory.name) / "runs.sqlite3"), epoch_s=lambda: self.clock, busy_s=2.0
        )
        self.mine, self.other = new_run_id(), new_run_id()
        for run_id, supervisor, leader in (
            (self.mine, ProcessIdentity(5001, 10.0), ProcessIdentity(5002, 11.0)),
            (self.other, OTHER_SUPERVISOR, OTHER_LEADER),
        ):
            self.store.accept(
                AcceptedRun(run_id, "/work/example", "task", Limits(60.0, 1024), ROUTING)
            )
            self.store.claim(run_id, supervisor)
            self.store.set_process(run_id, leader)


class NamesTests(IdentityCase):
    """Which identities are named for a run being cleaned up."""

    def test_the_supervisor_and_the_shim_leader_of_the_other_run_are_named(self) -> None:
        """Both processes of every other unfinished run are protected."""
        named = live_run_identities(self.store)(self.mine.value)
        self.assertIn(ProcessKey(6001, 1500.0), named, msg="the other supervisor")
        self.assertIn(ProcessKey(6002, 1501.0), named, msg="the other shim leader")

    def test_the_run_being_cleaned_does_not_protect_its_own_processes(self) -> None:
        """Its own supervisor and leader are not exempt from its own cleanup."""
        named = live_run_identities(self.store)(self.mine.value)
        self.assertNotIn(ProcessKey(5001, 10.0), named, msg="not its own supervisor")
        self.assertNotIn(ProcessKey(5002, 11.0), named, msg="not its own leader")


class ComposedCleanupTests(IdentityCase):
    """The names do their job inside a cleanup built the way the composition root builds it."""

    def test_another_runs_supervisor_standing_in_this_runs_folder_is_never_signalled(self) -> None:
        """A path match must not reach a live run's supervisor or shim leader."""
        self.clock = NOW
        self.store.set_state(self.mine, RunState.FINALIZING)
        self.store.quarantine(self.mine, KEPT, ())
        sandbox = str(self.run_area / self.mine.sandbox_name)
        world = FakeWorld()
        for identity in (OTHER_SUPERVISOR, OTHER_LEADER):
            key = ProcessKey(identity.pgid, identity.start_time_s or 0.0)
            world.add(FakeProcess(key=key, ppid=1, pgid=key.pid, cwd=sandbox))
        cleanup = ContainmentCleanup(
            world, world, FakeSleeper(FakeClock()), ObservationSettings(1.0, 2.0, 2.0, 5.0, 2, 8)
        )
        look = QuarantineLook(
            self.store,
            LibprocProcessTable(),
            run_area=str(self.run_area),
            containment=cleanup.run,
            protected=live_run_identities(self.store),
        )
        look.look(self.mine)
        self.assertEqual(world.signals, [], msg="nothing of the other run was signalled")


if __name__ == "__main__":
    unittest.main()
