"""Tests of the real sleeper used by the wait loops."""

import unittest
from unittest import mock

from agents_dispatcher.adapters.system.system_sleeper import SystemSleeper


class SystemSleeperTests(unittest.TestCase):
    """The sleeper blocks for the asked time and nothing else."""

    def test_it_sleeps_for_the_requested_seconds(self) -> None:
        """The wait loops rely on the pause being the one they computed."""
        with mock.patch("time.sleep") as sleep:
            SystemSleeper().sleep(0.25)
        sleep.assert_called_once_with(0.25)

    def test_a_negative_pause_is_not_passed_on(self) -> None:
        """`time.sleep` raises on a negative value; a wait loop must never crash on rounding."""
        with mock.patch("time.sleep") as sleep:
            SystemSleeper().sleep(-0.1)
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
