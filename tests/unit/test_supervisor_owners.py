"""Tests that the supervisor's owner lock tells the run's log when its sign of life fails."""

import unittest
from unittest import mock

from agents_dispatcher.composition import supervisor
from agents_dispatcher.composition.wiring import CONTROL_DIR


class OwnerWiringTests(unittest.TestCase):
    """How the supervisor builds the lock that holds its run."""

    def test_a_failing_beat_is_reported_to_the_log_of_the_run(self) -> None:
        """A sign of life that stops being written must reach the run log, not vanish."""
        lines: list[str] = []
        with mock.patch.object(supervisor, "FileRunOwners") as owners_class:
            supervisor.build_owners("/state", lines.append)
        owners_class.assert_called_once_with(f"/state/{CONTROL_DIR}", on_beat_failure=lines.append)


if __name__ == "__main__":
    unittest.main()
