"""Tests of the resource limits the watch enforces on a whole run (O1 layer 5)."""

import threading
from collections.abc import Sequence

from agents_dispatcher.application.ports import DiskUsage
from agents_dispatcher.application.settings import WalkSettings
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
        self.knobs.output_bytes = MIB
        outcome = self.execute(
            "import sys, time; sys.stdout.write('x' * (3 * 1024 * 1024)); sys.stdout.flush(); "
            "time.sleep(120)"
        )
        self.assertEqual(outcome.limit_hit, LimitKind.OUTPUT, msg="the output limit tripped")
        self.assertEqual(outcome.unresolved, (), msg="and the worker was stopped")

    def test_the_two_streams_count_together(self) -> None:
        """Each stream stays under the limit; together they pass it."""
        self.knobs.output_bytes = MIB
        outcome = self.execute(
            "import sys, time\n"
            "sys.stdout.write('x' * (700 * 1024)); sys.stdout.flush()\n"
            "sys.stderr.write('y' * (700 * 1024)); sys.stderr.flush()\n"
            "time.sleep(120)\n"
        )
        self.assertEqual(outcome.limit_hit, LimitKind.OUTPUT, msg="stdout and stderr summed")

    def test_a_worker_under_the_limit_is_left_alone(self) -> None:
        """A hundred kibibytes against a one mebibyte limit."""
        self.knobs.output_bytes = MIB
        outcome = self.execute("print('x' * (100 * 1024))")
        self.assertIsNone(outcome.limit_hit, msg="no limit tripped")
        self.assertEqual(outcome.exit_status, 0, msg="the worker finished by itself")


class AggregateDescriptorTests(ConfinementCase):
    """Descriptors are watched over the whole tree, not only the leader."""

    def test_a_tree_that_together_passes_the_limit_is_stopped(self) -> None:
        """Four children with about forty files each, against a limit of 120 for the tree."""
        self.knobs.aggregate_descriptors = 120
        outcome = self.execute(OPEN_MANY)
        self.assertEqual(outcome.limit_hit, LimitKind.DESCRIPTORS, msg="the sum tripped")
        self.assertEqual(outcome.unresolved, (), msg="and the tree was stopped")

    def test_a_tree_under_the_limit_is_left_alone(self) -> None:
        """The same tree against a roomy limit finishes when the worker is told to stop."""
        self.knobs.aggregate_descriptors = 100_000
        calls = iter(range(100))
        outcome = self.execute(OPEN_MANY, should_stop=lambda: next(calls) > STOP_AFTER_CALLS)
        self.assertIsNone(outcome.limit_hit, msg="no limit tripped")
        self.assertTrue(outcome.stopped, msg="stopped by the caller")


class DiskLimitTests(ConfinementCase):
    """The run area is bounded in allocated bytes and in inodes, measured by a real walk."""

    def setUp(self) -> None:
        """Walk fast, so that the tests do not wait for the shipped two seconds."""
        super().setUp()
        self.knobs.walk_settings = WalkSettings(interval_s=0.1, maximum_age_s=5.0)

    def test_a_worker_that_writes_past_the_byte_limit_is_stopped_with_that_kind(self) -> None:
        """Three mebibytes in the worktree against a limit of one."""
        self.knobs.sandbox_bytes = MIB
        outcome = self.execute(
            "import time\n"
            "with open('big.bin', 'wb') as handle:\n"
            "    handle.write(bytes(3 * 1024 * 1024))\n"
            "time.sleep(120)\n"
        )
        self.assertEqual(outcome.limit_hit, LimitKind.SANDBOX_BYTES, msg="bytes limit tripped")
        self.assertEqual(outcome.unresolved, (), msg="and the worker was stopped")

    def test_a_worker_that_creates_files_past_the_inode_limit_is_stopped(self) -> None:
        """Three hundred files against a limit of two hundred."""
        self.knobs.sandbox_inodes = 200
        outcome = self.execute(
            "import pathlib, time\n"
            "for number in range(300):\n"
            "    pathlib.Path(f'f{number}.txt').write_text('x')\n"
            "time.sleep(120)\n"
        )
        self.assertEqual(outcome.limit_hit, LimitKind.SANDBOX_INODES, msg="inode limit tripped")

    def test_a_worker_under_both_limits_is_left_alone(self) -> None:
        """A few small files against generous limits."""
        self.knobs.sandbox_bytes = 100 * MIB
        self.knobs.sandbox_inodes = 10_000
        outcome = self.execute(
            "import pathlib\n"
            "for number in range(5):\n"
            "    pathlib.Path(f'f{number}.txt').write_text('x')\n"
            "import time; time.sleep(1.5)\n"
        )
        self.assertIsNone(outcome.limit_hit, msg="no limit tripped")
        self.assertEqual(outcome.exit_status, 0, msg="the worker finished by itself")


class StalledWalk:
    """A walk that never answers until the test lets it go."""

    def __init__(self) -> None:
        """Create the event the walk waits on."""
        self.release = threading.Event()

    def measure(self, roots: Sequence[str]) -> DiskUsage:  # noqa: ARG002 - the stall ignores them
        """Wait, then report nothing."""
        self.release.wait(timeout=30.0)
        return DiskUsage(0, 0)


class ObservationLossTests(ConfinementCase):
    """A run that cannot be measured is stopped, and the loss is reported."""

    def test_a_stalled_disk_walk_stops_the_run_with_the_loss_named(self) -> None:
        """The loop enforces the maximum age of the walk with its own clock."""
        stalled = StalledWalk()
        self.addCleanup(stalled.release.set)
        self.knobs.walk = stalled
        self.knobs.walk_settings = WalkSettings(interval_s=0.1, maximum_age_s=1.0)
        outcome = self.execute("import time; time.sleep(120)")
        self.assertIn("disk walk", outcome.observation_lost or "", msg="the loss is named")
        self.assertIsNone(outcome.limit_hit, msg="and it is not a measured breach")
        self.assertEqual(outcome.unresolved, (), msg="the worker was stopped")
