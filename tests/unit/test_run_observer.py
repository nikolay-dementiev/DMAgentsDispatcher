"""Tests of the run-time observer: the descendant ledger and the memory of the tracked tree."""

import unittest

from agents_dispatcher.application.containment import RunObserver
from agents_dispatcher.application.settings import ObservationSettings
from agents_dispatcher.domain.attribution import RunFacts
from agents_dispatcher.domain.process import ProcessKey
from tests.support.clock import FakeClock
from tests.support.fake_world import FakeProcess, FakeWorld

MARKER = "run-1"
SHIM = ProcessKey(100, 1000.0)
LATER = 1001.0
MIB = 1_048_576  # the weight of every live process in the fake world
SETTINGS = ObservationSettings(1.0, 2.0, 2.0, 5.0, 2, 8)


def facts() -> RunFacts:
    """Build the facts of run-1: shim 100 leads group 100."""
    return RunFacts(
        marker=MARKER,
        shim=SHIM,
        group=100,
        roots=("/state/runs/run-1",),
        remembered=frozenset(),
        protected_pids=frozenset({0, 1}),
        protected_keys=frozenset(),
        path_until=None,
    )


def child_of_shim(pid: int) -> FakeProcess:
    """Build a process whose parent is the shim and which sits in another group."""
    return FakeProcess(key=ProcessKey(pid, LATER), ppid=SHIM.pid, pgid=pid)


class ObserverTests(unittest.TestCase):
    """What the watch sees between two scans."""

    def setUp(self) -> None:
        """Create a world, a clock and an observer over them."""
        super().setUp()
        self.world = FakeWorld()
        self.clock = FakeClock()
        self.observer = RunObserver(self.world, self.clock, SETTINGS, facts())

    def test_the_first_call_looks_at_once_and_sums_the_tracked_tree(self) -> None:
        """Two descendants of the shim weigh two units; nothing waits for an interval."""
        self.world.add(child_of_shim(200))
        self.world.add(child_of_shim(201))
        self.assertEqual(self.observer.memory(), 2 * MIB, "memory of the tree")

    def test_no_new_look_is_taken_before_the_interval_has_passed(self) -> None:
        """The table is read at most once per process interval."""
        self.world.add(child_of_shim(200))
        self.observer.memory()
        self.clock.advance(0.5)
        self.observer.memory()
        self.assertEqual(self.world.looks, 1, "still the first look")
        self.clock.advance(0.6)
        self.observer.memory()
        self.assertEqual(self.world.looks, 2, "a second look after the interval")

    def test_paths_are_read_only_at_the_path_interval(self) -> None:
        """The dear read of working directories and open files runs on its own, slower clock."""
        self.observer.memory()
        for _ in range(3):
            self.clock.advance(1.0)
            self.observer.memory()
        self.assertEqual(self.world.looks, 4, "four looks in three seconds")
        self.assertEqual(self.world.path_looks, 2, "paths at 0 s and at 2 s only")

    def test_a_descendant_that_hides_afterwards_still_counts(self) -> None:
        """Remembered by pid and start time, it counts after its parent and marker are gone."""
        hidden = self.world.add(child_of_shim(200))
        self.observer.memory()
        hidden.ppid = 1
        hidden.pgid = 200
        self.clock.advance(1.5)
        self.assertEqual(self.observer.memory(), MIB, "the hidden process is still weighed")
        self.assertIn(hidden.key, self.observer.remembered, "and remembered")

    def test_descriptors_are_summed_over_the_tracked_tree_hidden_ones_included(self) -> None:
        """Together the processes hold more than any one of them: the sum is what is watched."""
        self.world.add(child_of_shim(200)).descriptors = 40
        hidden = self.world.add(child_of_shim(201))
        hidden.descriptors = 70
        self.observer.memory()
        hidden.ppid = 1
        hidden.pgid = 201
        self.clock.advance(1.5)
        self.assertEqual(self.observer.descriptors(), 110, "forty and seventy, the hidden one too")

    def test_a_process_that_ended_no_longer_counts(self) -> None:
        """A remembered key is weighed only while that process is alive."""
        gone = self.world.add(child_of_shim(200))
        self.observer.memory()
        gone.alive = False
        self.clock.advance(1.5)
        self.assertEqual(self.observer.memory(), 0, "nothing left to weigh")

    def test_a_process_look_that_stays_incomplete_past_its_maximum_age_is_a_loss(self) -> None:
        """Two seconds is the maximum age of the process look in these settings."""
        self.observer.memory()
        self.world.incomplete_from = 2
        self.clock.advance(1.5)
        self.observer.memory()
        self.assertIsNone(self.observer.lost(), "one incomplete look is not yet a loss")
        self.clock.advance(1.0)
        self.observer.memory()
        lost = self.observer.lost()
        self.assertIn("process", lost or "", "the process look is named")

    def test_a_path_look_older_than_its_maximum_age_is_a_loss_though_the_process_look_is_fresh(
        self,
    ) -> None:
        """Five seconds is the maximum age of the path look; the cheap look keeps succeeding."""
        self.observer.memory()
        self.world.paths_incomplete_from = 2
        for _ in range(6):
            self.clock.advance(1.0)
            self.observer.memory()
        lost = self.observer.lost()
        self.assertIn("path", lost or "", "the path look is named")

    def test_complete_looks_are_never_a_loss(self) -> None:
        """A long run that keeps being looked at loses nothing."""
        for _ in range(20):
            self.clock.advance(1.0)
            self.observer.memory()
        self.assertIsNone(self.observer.lost(), "nothing lost")


if __name__ == "__main__":
    unittest.main()
