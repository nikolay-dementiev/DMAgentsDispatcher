"""Tests of the disk watch: a walk on its own thread whose age the watch loop enforces."""

import threading
import unittest
from collections.abc import Sequence

from agents_dispatcher.adapters.system.disk_watch import DiskWatch
from agents_dispatcher.application.ports import DiskUsage
from agents_dispatcher.application.settings import WalkSettings
from tests.support.clock import FakeClock

WAIT_S = 10.0
SETTINGS = WalkSettings(interval_s=0.01, maximum_age_s=6.0)
ROOTS = ("/state/runs/run-1",)


class GatedWalk:
    """A walk that runs once per permit the test hands out and records every call."""

    def __init__(self) -> None:
        """Start with no permit: the first walk waits."""
        self.gate = threading.Semaphore(0)
        self.finished = threading.Semaphore(0)
        self.usage = DiskUsage(allocated_bytes=100, inodes=7)
        self.failure: OSError | None = None
        self.roots: Sequence[str] = ()

    def measure(self, roots: Sequence[str]) -> DiskUsage:
        """Wait for a permit, then answer or fail as scripted."""
        self.roots = roots
        self.gate.acquire(timeout=WAIT_S)
        try:
            if self.failure is not None:
                raise self.failure
            return self.usage
        finally:
            self.finished.release()

    def allow_one_and_wait(self) -> None:
        """Let one walk run and wait until it has ended."""
        self.gate.release()
        if not self.finished.acquire(timeout=WAIT_S):
            message = "the walk did not finish"
            raise AssertionError(message)


class DiskWatchTests(unittest.TestCase):
    """What the watch loop reads from the walk."""

    def setUp(self) -> None:
        """Create the walk, the clock and a started watch; stop everything afterwards."""
        super().setUp()
        self.walk = GatedWalk()
        self.clock = FakeClock()
        self.watch = DiskWatch(self.walk, self.clock, SETTINGS, ROOTS)
        self.addCleanup(self.watch.stop)
        self.addCleanup(self.release_walk)
        self.watch.start()

    def release_walk(self) -> None:
        """Let any waiting walk end so the thread can be joined."""
        for _ in range(50):
            self.walk.gate.release()

    def test_nothing_is_known_before_the_first_walk_ends_and_that_is_not_a_loss(self) -> None:
        """The walk of a large tree takes a while; its absence is only a problem when old."""
        reading = self.watch.reading()
        self.assertIsNone(reading.usage, "no usage yet")
        self.assertIsNone(reading.lost, "and nothing lost")

    def test_the_usage_of_a_finished_walk_is_read(self) -> None:
        """The roots reach the walk and its answer reaches the loop."""
        self.walk.allow_one_and_wait()
        reading = self.watch.reading()
        self.assertEqual(reading.usage, DiskUsage(100, 7), "usage of the walk")
        self.assertEqual(tuple(self.walk.roots), ROOTS, "the roots were passed on")

    def test_a_walk_that_never_ends_is_a_loss_once_it_is_older_than_the_maximum_age(self) -> None:
        """The loop enforces the age with its own clock; the stalled thread cannot hide itself."""
        self.clock.advance(5.9)
        self.assertIsNone(self.watch.reading().lost, "still within the maximum age")
        self.clock.advance(0.2)
        lost = self.watch.reading().lost
        self.assertIsNotNone(lost, "older than the maximum age")
        self.assertIn("disk walk", lost or "", "the text names what was lost")

    def test_the_age_counts_from_the_last_walk_that_ended(self) -> None:
        """A walk that ended long ago is as stale as none."""
        self.walk.allow_one_and_wait()
        self.clock.advance(7.0)
        self.assertIsNotNone(self.watch.reading().lost, "the last walk is too old")

    def test_a_walk_that_fails_is_a_loss_at_once(self) -> None:
        """An error is not waited out: what cannot be measured is reported."""
        self.walk.failure = OSError("permission denied")
        self.walk.allow_one_and_wait()
        lost = self.watch.reading().lost
        self.assertIn("permission denied", lost or "", "the error is in the text")

    def test_a_later_good_walk_clears_an_earlier_failure(self) -> None:
        """The failure belongs to the walk that had it."""
        self.walk.failure = OSError("permission denied")
        self.walk.allow_one_and_wait()
        self.walk.failure = None
        self.walk.allow_one_and_wait()
        self.assertIsNone(self.watch.reading().lost, "recovered")


if __name__ == "__main__":
    unittest.main()
