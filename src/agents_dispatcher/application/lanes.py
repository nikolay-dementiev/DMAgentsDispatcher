"""The lanes of work and the port that runs jobs on them."""

import enum
from collections.abc import Callable
from typing import Protocol


class Lane(enum.Enum):
    """A class of work with its own workers and its own queue."""

    CONTROL = "control"
    NORMAL = "normal"


class LaneRunner(Protocol):
    """Runs jobs on bounded, separate lanes."""

    def submit(self, lane: Lane, job: Callable[[], None]) -> bool:
        """Queue a job; return False when the lane is full or closed."""
        ...

    def close(self) -> None:
        """Stop accepting jobs and drop those that have not started."""
        ...
