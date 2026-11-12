"""An injected clock for tests: time moves only when the test says so."""


class FakeClock:
    """A monotonic clock that stands still until advanced."""

    def __init__(self, start: float = 0.0) -> None:
        """Start the clock at ``start`` seconds."""
        self._now = start

    def now(self) -> float:
        """Return the current time in seconds; reading never moves it."""
        return self._now

    def advance(self, seconds: float) -> None:
        """Move time forward by ``seconds``; a negative step is a test bug."""
        if seconds < 0:
            message = f"a clock cannot go backwards (got {seconds})"
            raise ValueError(message)
        self._now += seconds
