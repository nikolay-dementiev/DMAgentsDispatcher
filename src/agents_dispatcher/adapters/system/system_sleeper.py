"""The real sleeper: blocks the calling thread for a time the wait loops computed."""

import time


class SystemSleeper:
    """A `Sleeper` backed by `time.sleep`."""

    def sleep(self, seconds: float) -> None:
        """Block for about `seconds`; a zero or negative pause (rounding in a loop) is a no-op."""
        if seconds > 0:
            time.sleep(seconds)
