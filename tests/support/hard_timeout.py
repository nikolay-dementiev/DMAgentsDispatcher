"""A hard per-test time limit for tests that wait on real processes (F7).

A test that waits for a child can hang forever when the code under test is broken. The limit
turns such a hang into a failure of that one test, so a mutation proof can never stall a run.
"""

import signal
import types
import unittest

TEST_LIMIT_S = 90


class HardTimeoutCase(unittest.TestCase):
    """A test case that fails, instead of hanging, after `TEST_LIMIT_S` seconds."""

    def setUp(self) -> None:
        """Arm an alarm that raises in the test's own thread."""
        super().setUp()
        previous = signal.signal(signal.SIGALRM, self._expired)
        self.addCleanup(signal.signal, signal.SIGALRM, previous)
        signal.setitimer(signal.ITIMER_REAL, TEST_LIMIT_S)
        self.addCleanup(signal.setitimer, signal.ITIMER_REAL, 0)

    def _expired(self, _signal_number: int, _frame: types.FrameType | None) -> None:
        """Fail the running test."""
        message = f"test exceeded its hard limit of {TEST_LIMIT_S} s"
        raise AssertionError(message)
