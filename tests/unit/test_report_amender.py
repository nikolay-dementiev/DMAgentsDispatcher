"""Tests of the amender that rewrites a stored report after a quarantine is resolved."""

import json
import unittest
from dataclasses import replace

from agents_dispatcher.adapters.system.json_report import JsonReportAmender, report_text
from agents_dispatcher.domain.run import RunReport, RunState, TerminalReason
from tests.support.cli_book import make_report

UNCERTAIN = "CLEANUP_UNCERTAIN: a worker process could not be ended or shown gone (pid 9)"
NOT_SEALED = "result not sealed: a worker process is still alive"


def kept_report() -> str:
    """Build the report a quarantined run keeps: terminal in form, with the two live notes."""
    report: RunReport = make_report(TerminalReason.COMPLETED)
    return report_text(
        replace(
            report,
            branch=None,
            commits_on_branch=0,
            notes=("proxy: none", NOT_SEALED, UNCERTAIN),
            problem="CLEANUP_UNCERTAIN",
        )
    )


class AmenderTests(unittest.TestCase):
    """What changes in a report when the quarantine ends."""

    def test_a_resolved_report_gets_the_result_and_the_note_and_loses_the_open_problem(
        self,
    ) -> None:
        """The two notes that were true while the run was held are no longer true."""
        text = JsonReportAmender().amend(
            kept_report(),
            branch="agentsd-run-x",
            commits=1,
            notes=("absence verified",),
            resolved=True,
        )
        shown = json.loads(text)
        self.assertEqual(
            (shown["branch"], shown["commits_on_branch"], shown["state"]),
            ("agentsd-run-x", 1, "TERMINAL"),
            msg="the result and the state",
        )
        self.assertIsNone(shown["problem"], msg="the problem is closed")
        self.assertEqual(shown["notes"], ["proxy: none", "absence verified"], msg="notes")

    def test_a_released_report_keeps_the_problem_and_the_notes_that_are_still_true(self) -> None:
        """A release proves nothing: the survivors may be alive and the result is not sealed."""
        text = JsonReportAmender().amend(
            kept_report(), branch=None, commits=0, notes=("released by operator",), resolved=False
        )
        shown = json.loads(text)
        self.assertEqual(shown["problem"], "CLEANUP_UNCERTAIN", msg="the problem stays")
        self.assertEqual(
            shown["notes"],
            ["proxy: none", NOT_SEALED, UNCERTAIN, "released by operator"],
            msg="nothing true is dropped",
        )
        self.assertEqual(shown["state"], "TERMINAL", msg="still shown as terminal")

    def test_a_report_that_is_not_json_is_an_error(self) -> None:
        """A damaged kept report must not be papered over."""
        with self.assertRaises(ValueError, msg="not JSON"):
            JsonReportAmender().amend("not json", branch=None, commits=0, notes=(), resolved=True)

    def test_the_state_name_of_a_terminal_report_is_terminal(self) -> None:
        """Guards the constant the amender writes."""
        self.assertEqual(RunState.TERMINAL.name, "TERMINAL", msg="the name")


if __name__ == "__main__":
    unittest.main()
