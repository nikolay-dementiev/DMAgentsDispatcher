"""The production clock."""

import time


class SuspendInclusiveClock:
    """A monotonic clock that keeps counting while the machine sleeps (ARCH section 4.7).

    On macOS `time.monotonic()` is `mach_absolute_time`, which stops during sleep, so a
    deadline measured with it would stretch by the time the machine slept. `CLOCK_MONOTONIC`
    maps to `mach_continuous_time` and does not stop.
    """

    def now(self) -> float:
        """Return seconds on a suspend-inclusive monotonic scale."""
        return time.clock_gettime(time.CLOCK_MONOTONIC)
