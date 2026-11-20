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

    def test_a_process_that_ended_no_longer_counts(self) -> None:
        """A remembered key is weighed only while that process is alive."""
        gone = self.world.add(child_of_shim(200))
        self.observer.memory()
        gone.alive = False
        self.clock.advance(1.5)
        self.assertEqual(self.observer.memory(), 0, "nothing left to weigh")


if __name__ == "__main__":
    unittest.main()
