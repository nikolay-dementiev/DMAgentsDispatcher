"""Tests of the start hook: abandoned runs are swept, then held runs get another look."""

import unittest

from agents_dispatcher.application.quarantine_resolution import Resolution, SweepAndRecheck
from agents_dispatcher.domain.run import RunId
from tests.support.run_spies import RUN_ID, Journal


class Sweeper:
    """Records the sweep and reports one closed run."""

    def __init__(self, journal: Journal) -> None:
        """Keep the shared journal."""
        self._journal = journal

    def sweep(self) -> list[RunId]:
        """Note the sweep and name the run it closed."""
        self._journal.add("sweep")
        return [RUN_ID]


class Rechecker:
    """Records the re-check."""

    def __init__(self, journal: Journal) -> None:
        """Keep the shared journal."""
        self._journal = journal

    def recheck_all(self) -> list[Resolution]:
        """Note the re-check."""
        self._journal.add("recheck")
        return []


class SweepAndRecheckTests(unittest.TestCase):
    """What the start hook does and in which order."""

    def test_the_sweep_runs_first_then_the_recheck_and_the_closed_runs_are_returned(self) -> None:
        """A run the sweep sends to quarantine is looked at again right away."""
        journal = Journal()
        hook = SweepAndRecheck(Sweeper(journal), Rechecker(journal))
        closed = hook.sweep()
        self.assertEqual(journal.entries, ["sweep", "recheck"], msg="sweep before recheck")
        self.assertEqual(closed, [RUN_ID], msg="the sweep's answer is passed on")


if __name__ == "__main__":
    unittest.main()
