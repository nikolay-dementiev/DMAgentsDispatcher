"""A stored report that is not a JSON object is a failure on the command line, as it is on MCP.

Owner decision 2026-11-22 (KICKOFF-13 D5): `report` and `run` read the stored text through the
same check as the `report` tool. A damaged text is REPORT_UNAVAILABLE, exit 25; a valid text is
printed exactly as stored.
"""

import io
import json
import unittest

from agents_dispatcher.application.run_queries import ReportResult
from agents_dispatcher.domain.run import TerminalReason
from agents_dispatcher.interfaces.cli.app import main
from tests.support.cli_book import (
    Book,
    FakeReport,
    FakeWait,
    FixedVersion,
    ended,
    make_report,
)
from tests.support.cli_failure import failure_of
from tests.support.run_spies import RUN_ID

ID = RUN_ID.value
RUN = ["run", "--repo", "/work/example", "--task", "add a function"]
DAMAGED = ("[1, 2]", "not json at all", '"a string"', "null", "")


def run_command(argv: list[str], book: Book) -> tuple[int, str, str]:
    """Run a command against the fakes and return its exit code, stdout and stderr."""
    stdout, stderr = io.StringIO(), io.StringIO()
    code = main(argv, stdout, stderr, FixedVersion(), book.services, book.scope)
    return code, stdout.getvalue(), stderr.getvalue()


def book_with(text: str) -> Book:
    """Return a book whose run completed and whose stored report is `text`."""
    return Book(
        wait=FakeWait([ended(TerminalReason.COMPLETED)]),
        report=FakeReport(result=ReportResult(report_json=text, pending=None)),
    )


class DamagedReportTests(unittest.TestCase):
    """Stored text that is not a JSON object."""

    def test_report_and_run_exit_twenty_five_with_the_failure_object(self) -> None:
        """Nothing damaged reaches stdout as if it were a report."""
        for argv in (["report", ID], RUN):
            for text in DAMAGED:
                with self.subTest(command=argv[0], text=text):
                    code, stdout, stderr = run_command(argv, book_with(text))
                    self.assertEqual(code, 25, msg="the report is unavailable")
                    self.assertEqual(
                        failure_of(stdout)["code"], "REPORT_UNAVAILABLE", msg="the code"
                    )
                    self.assertIn("stored report", stderr, msg="stderr says what is wrong")


class ValidReportTests(unittest.TestCase):
    """A valid stored text is printed as stored, not rebuilt."""

    def test_a_report_with_its_own_spacing_is_printed_byte_for_byte(self) -> None:
        """A re-dump would change the spacing; the stored text must not change."""
        report = make_report(TerminalReason.COMPLETED).as_dict()
        text = json.dumps(report, separators=(",", ":"))
        for argv in (["report", ID], RUN):
            with self.subTest(command=argv[0]):
                code, stdout, stderr = run_command(argv, book_with(text))
                self.assertEqual(code, 0, msg="a completed run exits 0")
                self.assertEqual(stdout, text + "\n", msg="the stored text, unchanged")
                self.assertEqual(stderr, "", msg="nothing on stderr")


if __name__ == "__main__":
    unittest.main()
