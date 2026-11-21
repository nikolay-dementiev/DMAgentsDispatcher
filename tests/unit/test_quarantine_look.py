"""Tests of the look at a held run: the facts it builds and how it reads the cleanup result."""

import tempfile
import unittest
from pathlib import Path

from agents_dispatcher.adapters.system.process_table import LibprocProcessTable
from agents_dispatcher.adapters.system.quarantine_look import QuarantineLook
from agents_dispatcher.adapters.system.sqlite_run_store import SqliteRunStore
from agents_dispatcher.application.containment import CleanupResult, CleanupVerdict
from agents_dispatcher.application.ports import AcceptedRun, FinishRecord, ProcessIdentity
from agents_dispatcher.domain.attribution import Attributed, RunFacts, Source
from agents_dispatcher.domain.process import ProcessKey
from agents_dispatcher.domain.run import Limits, RunState, Survivor, TerminalReason
from tests.support.routing import ROUTING
from tests.support.run_spies import RUN_ID

QUARANTINED_AT = 2000.5
SURVIVORS = (
    Survivor(pid=901, start_time=1234.5, sources=("group", "marker")),
    Survivor(pid=902, start_time=1240.25, sources=("folder",)),
)
KEPT = FinishRecord(TerminalReason.COMPLETED, None, 0, None, 0, 3.5, "{}")


class LookCase(unittest.TestCase):
    """A store holding the default run in quarantine, and a look over a scripted cleanup."""

    def setUp(self) -> None:
        """Quarantine the default run at a known time."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.run_area = Path(directory.name).resolve() / "runs"
        self.store = SqliteRunStore(
            str(Path(directory.name) / "runs.sqlite3"), epoch_s=lambda: QUARANTINED_AT, busy_s=2.0
        )
        self.store.accept(AcceptedRun(RUN_ID, "/work/example", "task", Limits(60.0, 1024), ROUTING))
        self.store.claim(RUN_ID, ProcessIdentity(pgid=4321, start_time_s=55.5))
        self.store.set_state(RUN_ID, RunState.FINALIZING)
        self.store.quarantine(RUN_ID, KEPT, SURVIVORS)
        self.seen: list[RunFacts] = []
        self.answer = CleanupResult(CleanupVerdict.OBSERVED_EMPTY, 2, (), (), (), frozenset())

    def cleanup(self, facts: RunFacts) -> CleanupResult:
        """Record the facts and return the scripted answer."""
        self.seen.append(facts)
        return self.answer

    def look(self) -> QuarantineLook:
        """Build the look under test."""
        return QuarantineLook(
            self.store,
            LibprocProcessTable(),
            run_area=str(self.run_area),
            containment=self.cleanup,
            protected=lambda _marker: frozenset(),
        )


class UnreadableTests(LookCase):
    """What the cleanup could not rule out is passed on, not dropped."""

    def test_unreadable_processes_reach_the_result_by_name(self) -> None:
        """An empty verdict still carries the later processes whose paths were refused."""
        self.answer = CleanupResult(
            CleanupVerdict.OBSERVED_EMPTY, 2, (), (), (), frozenset(), (ProcessKey(5, 1.0),)
        )
        self.assertEqual(self.look().look(RUN_ID).unreadable, ("pid 5",), msg="named")


class ResidualTests(LookCase):
    """A process the cleanup must not signal but that holds the sandbox is reported by name."""

    def test_a_residual_holding_the_folder_reaches_the_result_by_name(self) -> None:
        """Empty of the run's own processes, yet not free to remove."""
        self.answer = CleanupResult(
            CleanupVerdict.OBSERVED_EMPTY, 2, (), (), (ProcessKey(7, 1.0),), frozenset()
        )
        found = self.look().look(RUN_ID)
        self.assertEqual(found.residual, ("pid 7",), msg="named")
        self.assertTrue(found.empty, msg="and the run itself is still shown empty")


class FactsTests(LookCase):
    """What the look tells the cleanup about the run."""

    def test_the_facts_hold_the_survivors_the_marker_the_folders_and_the_upper_bound(self) -> None:
        """The persisted survivors are remembered; folder matches stop at the quarantine time."""
        self.look().look(RUN_ID)
        facts = self.seen[0]
        self.assertEqual(facts.marker, RUN_ID.value, msg="the run marker")
        self.assertEqual(
            facts.remembered,
            frozenset({ProcessKey(901, 1234.5), ProcessKey(902, 1240.25)}),
            msg="the survivors are remembered by pid and start time",
        )
        self.assertEqual(facts.path_until, QUARANTINED_AT, msg="a later process is not the run's")
        sandbox = (self.run_area / RUN_ID.sandbox_name).resolve()
        self.assertEqual(
            facts.roots, (str(sandbox), str(sandbox / "worktree")), msg="the kept folders"
        )

    def test_a_group_that_is_not_provably_the_runs_is_not_attributed(self) -> None:
        """The stored leader is long gone and its start time is not checkable here."""
        self.look().look(RUN_ID)
        self.assertEqual(self.seen[0].group, -1, msg="no group")


class ResultTests(LookCase):
    """How the cleanup's verdict becomes the answer."""

    def test_an_observed_empty_run_is_reported_empty(self) -> None:
        """Nothing is left."""
        found = self.look().look(RUN_ID)
        self.assertEqual((found.empty, found.survivors), (True, ()), msg="empty")

    def test_a_cleanup_that_is_uncertain_reports_the_survivors_with_their_sources(self) -> None:
        """What could not be ended or shown gone is named."""
        stubborn = Attributed(ProcessKey(4242, 777.0), frozenset({Source.MARKER, Source.GROUP}))
        self.answer = CleanupResult(
            CleanupVerdict.CLEANUP_UNCERTAIN, 8, (), (stubborn,), (), frozenset()
        )
        found = self.look().look(RUN_ID)
        self.assertFalse(found.empty, msg="not empty")
        self.assertEqual(
            found.survivors, (Survivor(4242, 777.0, ("group", "marker")),), msg="the survivor"
        )


if __name__ == "__main__":
    unittest.main()
