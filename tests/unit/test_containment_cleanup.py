"""Tests of the cleanup rounds of the O1 layer 3: stop first, kill, observe, give up honestly."""

import unittest

from agents_dispatcher.application.containment import CleanupVerdict, ContainmentCleanup
from agents_dispatcher.application.settings import ObservationSettings
from agents_dispatcher.domain.attribution import RunFacts
from agents_dispatcher.domain.process import ProcessKey
from tests.support.clock import FakeClock
from tests.support.fake_world import FakeProcess, FakeWorld
from tests.support.run_book import FakeSleeper

MARKER = "run-1"
SANDBOX = "/state/runs/run-1"
SHIM_START = 1000.0
LATER = 1001.0
EARLIER = 900.0


def facts() -> RunFacts:
    """Build the facts of run-1: shim 100 started at 1000.0 and leads group 100."""
    return RunFacts(
        marker=MARKER,
        shim=ProcessKey(100, SHIM_START),
        group=100,
        roots=(SANDBOX,),
        remembered=frozenset(),
        protected_pids=frozenset({0, 1, 4242}),
        protected_keys=frozenset(),
        path_until=None,
    )


def straggler(pid: int, *, forks: bool = False, **flags: bool) -> FakeProcess:
    """Build a process outside the group that carries the run marker."""
    return FakeProcess(
        key=ProcessKey(pid, LATER),
        pgid=pid,
        marker=MARKER,
        forks=forks,
        immortal=flags.get("immortal", False),
        denied=flags.get("denied", False),
    )


def cleanup_over(world: FakeWorld, *, empty: int = 2, rounds: int = 8) -> ContainmentCleanup:
    """Build the cleanup over a fake world with a fake sleeper."""
    settings = ObservationSettings(1.0, 2.0, 2.0, 5.0, empty, rounds)
    return ContainmentCleanup(world, world, FakeSleeper(FakeClock()), settings)


class RoundsTests(unittest.TestCase):
    """How many looks it takes to call the run empty, and when it gives up."""

    def test_two_complete_empty_looks_end_as_observed_empty(self) -> None:
        """Nothing to find: `empty_rounds` consecutive complete looks, no signal."""
        world = FakeWorld()
        result = cleanup_over(world).run(facts())
        self.assertIs(result.verdict, CleanupVerdict.OBSERVED_EMPTY, "verdict")
        self.assertEqual(result.rounds, 2, "two empty rounds")
        self.assertEqual(world.signals, [], "nothing was signalled")

    def test_an_incomplete_look_never_counts_as_empty(self) -> None:
        """A table that could not be read completely proves nothing."""
        world = FakeWorld(incomplete_looks=1)
        result = cleanup_over(world).run(facts())
        self.assertIs(result.verdict, CleanupVerdict.OBSERVED_EMPTY, "verdict")
        self.assertEqual(result.rounds, 3, "the incomplete round plus two complete ones")

    def test_an_incomplete_look_between_empty_ones_restarts_the_count(self) -> None:
        """Two empty looks must be consecutive: an unreadable one between them breaks the run."""
        world = FakeWorld()
        world.incomplete_on = {2}
        result = cleanup_over(world).run(facts())
        self.assertEqual(result.rounds, 4, "empty, unreadable, empty, empty")

    def test_a_process_that_appears_after_an_empty_look_restarts_the_count(self) -> None:
        """Empty, found, then two more empty looks: the first empty look does not carry over."""
        world = FakeWorld()
        late: list[bool] = []

        def appear() -> None:
            if not late:
                late.append(True)
                world.add(straggler(200))

        world.after_look = appear
        result = cleanup_over(world).run(facts())
        self.assertEqual(result.rounds, 4, "empty, acting, empty, empty")

    def test_a_straggler_is_stopped_then_killed_then_two_empty_looks_follow(self) -> None:
        """Round 1 acts, rounds 2 and 3 observe: the acting round does not count as empty."""
        world = FakeWorld()
        world.add(straggler(200))
        result = cleanup_over(world).run(facts())
        self.assertIs(result.verdict, CleanupVerdict.OBSERVED_EMPTY, "verdict")
        self.assertEqual(result.rounds, 3, "one acting round and two empty ones")
        self.assertEqual(world.signals, [("STOP", 200), ("KILL", 200)], "stop before kill")
        self.assertEqual([item.pid for item in result.killed], [200], "reported as killed")

    def test_every_stop_comes_before_the_first_kill(self) -> None:
        """A process left running could fork while its sibling is being killed."""
        world = FakeWorld()
        world.add(straggler(200))
        world.add(straggler(201))
        cleanup_over(world).run(facts())
        names = [name for name, _ in world.signals]
        self.assertEqual(names, ["STOP", "STOP", "KILL", "KILL"], "stops first, then kills")

    def test_a_process_that_forks_endlessly_is_ended_by_stopping_it_first(self) -> None:
        """The stopped parent cannot fork again, so the family is finite and ends."""
        world = FakeWorld()
        world.add(straggler(200, forks=True))
        result = cleanup_over(world).run(facts())
        self.assertIs(result.verdict, CleanupVerdict.OBSERVED_EMPTY, "verdict")
        self.assertTrue(all(not p.alive for p in world.processes.values()), "everything ended")

    def test_a_process_that_will_not_die_ends_the_cleanup_after_the_last_round(self) -> None:
        """The bound is `cleanup_rounds`, exactly, and the survivor is named."""
        world = FakeWorld()
        world.add(straggler(200, immortal=True))
        result = cleanup_over(world, rounds=3).run(facts())
        self.assertIs(result.verdict, CleanupVerdict.CLEANUP_UNCERTAIN, "verdict")
        self.assertEqual(result.rounds, 3, "exactly cleanup_rounds rounds")
        self.assertEqual([item.key.pid for item in result.survivors], [200], "named as a survivor")


class MarkerSinceTests(unittest.TestCase):
    """The census is told which processes could have inherited the marker."""

    def test_every_look_of_the_cleanup_carries_the_start_time_of_the_shim(self) -> None:
        """An older process cannot have the marker, so its environment is not needed."""
        world = FakeWorld()
        world.add(straggler(200))
        cleanup_over(world).run(facts())
        self.assertEqual(
            set(world.marker_sinces), {facts().shim.start_time}, msg="all looks, the strike's too"
        )


class SafetyTests(unittest.TestCase):
    """What must never be signalled, and what a refusal means."""

    def test_a_process_of_someone_else_is_an_unresolved_survivor_at_once(self) -> None:
        """A refused signal cannot be retried into success: stop early, report it."""
        world = FakeWorld()
        world.add(straggler(200, denied=True))
        result = cleanup_over(world).run(facts())
        self.assertIs(result.verdict, CleanupVerdict.CLEANUP_UNCERTAIN, "verdict")
        self.assertEqual([item.key.pid for item in result.survivors], [200], "named")
        self.assertLess(result.rounds, 8, "it did not burn every round on a refusal")

    def test_a_pid_that_changed_hands_before_the_signal_is_left_alone(self) -> None:
        """The identity is revalidated at the signal, not only at the look."""
        world = FakeWorld()
        target = world.add(straggler(200))
        swapped: list[bool] = []

        def swap() -> None:
            if not swapped:
                swapped.append(True)
                world.replace_identity(200, LATER + 50)
                target.marker = None

        world.after_look = swap
        result = cleanup_over(world).run(facts())
        self.assertEqual(world.signals, [], "the stranger got no signal")
        self.assertTrue(target.alive and not target.stopped, "and it still runs")
        self.assertIs(result.verdict, CleanupVerdict.OBSERVED_EMPTY, "nothing of the run is left")

    def test_the_protected_pids_are_never_signalled(self) -> None:
        """The SDK's own process carries the marker in this scenario and still gets nothing."""
        world = FakeWorld()
        world.add(straggler(4242))
        result = cleanup_over(world).run(facts())
        self.assertEqual(world.signals, [], "no signal")
        self.assertIs(result.verdict, CleanupVerdict.OBSERVED_EMPTY, "verdict")

    def test_an_older_process_in_the_sandbox_is_reported_but_never_signalled(self) -> None:
        """The guard: the owner's shell that sat in the folder before the run is not a target."""
        world = FakeWorld()
        world.add(FakeProcess(key=ProcessKey(300, EARLIER), cwd=SANDBOX))
        result = cleanup_over(world).run(facts())
        self.assertEqual(world.signals, [], "no signal")
        self.assertEqual([item.pid for item in result.residual], [300], "reported as a residual")
        self.assertIs(result.verdict, CleanupVerdict.OBSERVED_EMPTY, "the run itself is empty")

    def test_a_later_process_with_denied_paths_is_reported_as_unreadable_not_signalled(
        self,
    ) -> None:
        """Observed empty, but the denial is carried in the evidence with the identity."""
        world = FakeWorld()
        world.add(FakeProcess(key=ProcessKey(400, LATER), paths_denied=True))
        result = cleanup_over(world).run(facts())
        self.assertEqual(world.signals, [], "no signal")
        self.assertEqual([item.pid for item in result.unreadable], [400], "listed")
        self.assertIs(result.verdict, CleanupVerdict.OBSERVED_EMPTY, "the run itself is empty")

    def test_an_older_process_with_denied_paths_is_not_reported(self) -> None:
        """The start guard: a denial of a process that was there before the run has no effect."""
        world = FakeWorld()
        world.add(FakeProcess(key=ProcessKey(401, EARLIER), paths_denied=True))
        result = cleanup_over(world).run(facts())
        self.assertEqual(result.unreadable, (), "not listed")


if __name__ == "__main__":
    unittest.main()
