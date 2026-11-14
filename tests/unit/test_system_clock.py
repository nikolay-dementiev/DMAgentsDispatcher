"""Tests of the suspend-inclusive system clock (ARCH section 4.7, F3)."""

import time
import unittest
from unittest import mock

from agents_dispatcher.adapters.system.system_clock import SuspendInclusiveClock


class SuspendInclusiveClockTests(unittest.TestCase):
    """The hard deadline must keep running while the machine sleeps."""

    def test_it_reads_clock_monotonic_not_the_sleep_blind_monotonic(self) -> None:
        """On macOS `time.monotonic` stops during sleep; `CLOCK_MONOTONIC` does not."""
        with (
            mock.patch("time.clock_gettime", return_value=1234.5) as continuous,
            mock.patch("time.monotonic", return_value=1.0),
        ):
            reading = SuspendInclusiveClock().now()
        self.assertEqual(reading, 1234.5, msg="the reading comes from clock_gettime")
        continuous.assert_called_once_with(time.CLOCK_MONOTONIC)

    def test_the_clock_moves_forward(self) -> None:
        """Two reads never go backwards."""
        clock = SuspendInclusiveClock()
        first = clock.now()
        self.assertGreaterEqual(clock.now(), first, msg="monotonic")


if __name__ == "__main__":
    unittest.main()
