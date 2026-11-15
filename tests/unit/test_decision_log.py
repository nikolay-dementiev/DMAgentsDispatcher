"""Tests of the SDK decision log: one private line per decision, written before anything exists."""

import stat
import tempfile
import unittest
from pathlib import Path

from agents_dispatcher.adapters.system.decision_log import FileDecisionLog

from agents_dispatcher.application.errors import DecisionLogError
from agents_dispatcher.domain.routing import Recommendation, Rejected, RouteMode, TaskMode
from tests.support.routing import REFUSED, STARTED, TASK


class DecisionLogTests(unittest.TestCase):
    """What the file holds and who may read it."""

    def setUp(self) -> None:
        """Create a private place for the log folder."""
        self._root = tempfile.TemporaryDirectory()
        self.addCleanup(self._root.cleanup)
        self.folder = Path(self._root.name) / "logs"
        self.log = FileDecisionLog(str(self.folder))

    def lines(self) -> list[str]:
        """Return the lines of the log."""
        return (self.folder / "sdk.log").read_text(encoding="utf-8").splitlines()

    def test_a_started_decision_is_one_line_with_every_evaluated_link(self) -> None:
        """Kind, task type, mode, request mode, and each link with its disposition and codes."""
        self.log.record(TASK, STARTED, RouteMode.ROUTE)
        (line,) = self.lines()
        for part in (
            "DECISION STARTED",
            "task_type=implement",
            "mode=write",
            "request=ROUTE",
            "openai:REFUSED(PROVIDER_NOT_LOADED)",
            "anthropic:SELECTED",
        ):
            self.assertIn(part, line, msg=part)

    def test_a_recommendation_and_a_rejection_are_named_as_such(self) -> None:
        """One line each, appended in order; no task type is written as a dash."""
        no_type = TASK.__class__(None, TaskMode.READ, TASK.chain)
        self.log.record(
            no_type,
            Recommendation((REFUSED,), model="m", effort="e", mode=TaskMode.READ, task_type=None),
            RouteMode.START,
        )
        self.log.record(no_type, Rejected((REFUSED,), task_type=None), RouteMode.ROUTE)
        first, second = self.lines()
        self.assertIn(
            "DECISION RECOMMENDATION task_type=- mode=read request=START",
            first,
            msg="recommendation",
        )
        self.assertIn(
            "DECISION REJECTED task_type=- mode=read request=ROUTE", second, msg="rejection"
        )

    def test_the_folder_and_the_file_are_private(self) -> None:
        """0700 and 0600: the log names task types and providers."""
        self.log.record(TASK, STARTED, RouteMode.ROUTE)
        self.assertEqual(stat.S_IMODE(self.folder.stat().st_mode), 0o700, msg="folder mode")
        mode = stat.S_IMODE((self.folder / "sdk.log").stat().st_mode)
        self.assertEqual(mode, 0o600, msg="file mode")

    def test_a_log_that_cannot_be_written_is_an_error_naming_the_file(self) -> None:
        """Never swallowed: the command that wanted the line fails with the cause."""
        blocker = Path(self._root.name) / "blocked"
        blocker.write_text("not a folder", encoding="utf-8")
        log = FileDecisionLog(str(blocker))
        with self.assertRaises(DecisionLogError, msg="unwritable") as caught:
            log.record(TASK, STARTED, RouteMode.ROUTE)
        self.assertIn("sdk.log", str(caught.exception), msg="names the file")


if __name__ == "__main__":
    unittest.main()
