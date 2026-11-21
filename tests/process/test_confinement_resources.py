"""Tests of the resource limits the watch enforces on a whole run (O1 layer 5)."""

from agents_dispatcher.domain.run import LimitKind
from tests.process.test_seatbelt_confinement import ConfinementCase

MIB = 1024 * 1024
STOP_AFTER_CALLS = 20  # the caller asks to stop on the twenty-first watch scan
OPEN_MANY = (
    "import subprocess, sys, time\n"
    "code = ('import time\\nfiles = [open(\"/dev/null\") for _ in range(40)]\\ntime.sleep(120)')\n"
    "for _ in range(4):\n"
    "    subprocess.Popen([sys.executable, '-B', '-c', code], stdin=subprocess.DEVNULL,\n"
    "                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
    "time.sleep(120)\n"
)


class OutputLimitTests(ConfinementCase):
    """What the worker prints is bounded, in total over both streams."""

    def test_a_worker_that_writes_past_the_limit_is_stopped_with_that_kind(self) -> None:
        """Three mebibytes against a one mebibyte limit."""
        self.output_bytes = MIB
        outcome = self.execute(
            "import sys, time; sys.stdout.write('x' * (3 * 1024 * 1024)); sys.stdout.flush(); "
            "time.sleep(120)"
        )
        self.assertEqual(outcome.limit_hit, LimitKind.OUTPUT, msg="the output limit tripped")
        self.assertEqual(outcome.survivors_alive, (), msg="and the worker was stopped")

    def test_the_two_streams_count_together(self) -> None:
        """Each stream stays under the limit; together they pass it."""
        self.output_bytes = MIB
        outcome = self.execute(
            "import sys, time\n"
            "sys.stdout.write('x' * (700 * 1024)); sys.stdout.flush()\n"
            "sys.stderr.write('y' * (700 * 1024)); sys.stderr.flush()\n"
            "time.sleep(120)\n"
        )
        self.assertEqual(outcome.limit_hit, LimitKind.OUTPUT, msg="stdout and stderr summed")

    def test_a_worker_under_the_limit_is_left_alone(self) -> None:
        """A hundred kibibytes against a one mebibyte limit."""
        self.output_bytes = MIB
        outcome = self.execute("print('x' * (100 * 1024))")
        self.assertIsNone(outcome.limit_hit, msg="no limit tripped")
        self.assertEqual(outcome.exit_status, 0, msg="the worker finished by itself")


class AggregateDescriptorTests(ConfinementCase):
    """Descriptors are watched over the whole tree, not only the leader."""

    def test_a_tree_that_together_passes_the_limit_is_stopped(self) -> None:
        """Four children with about forty files each, against a limit of 120 for the tree."""
        self.aggregate_descriptors = 120
        outcome = self.execute(OPEN_MANY)
        self.assertEqual(outcome.limit_hit, LimitKind.DESCRIPTORS, msg="the sum tripped")
        self.assertEqual(outcome.survivors_alive, (), msg="and the tree was stopped")

    def test_a_tree_under_the_limit_is_left_alone(self) -> None:
        """The same tree against a roomy limit finishes when the worker is told to stop."""
        self.aggregate_descriptors = 100_000
        calls = iter(range(100))
        outcome = self.execute(OPEN_MANY, should_stop=lambda: next(calls) > STOP_AFTER_CALLS)
        self.assertIsNone(outcome.limit_hit, msg="no limit tripped")
        self.assertTrue(outcome.stopped, msg="stopped by the caller")
