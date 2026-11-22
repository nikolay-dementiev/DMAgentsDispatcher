"""`--format json|text` and `--json` on the seven commands that print an object.

Owner decision 2026-11-22 (KICKOFF-13): JSON stays the default and is byte-identical with or
without a flag; `--format text` shows the same object for a person. The flag follows the command
name only, the last of `--json` and `--format` wins, and a failure is shown in the chosen format
once the arguments have parsed.
"""

import json
import unittest

from agents_dispatcher.application.errors import NoProviderEligibleError, UnknownRunError
from agents_dispatcher.application.failures import describe
from agents_dispatcher.application.run_queries import ReportResult
from agents_dispatcher.domain.routing import Rejected
from agents_dispatcher.domain.run import TerminalReason
from agents_dispatcher.interfaces.cli.text_view import render_text
from tests.support.cli_book import (
    Book,
    FakeReport,
    FakeStart,
    FakeStatus,
    FakeStop,
    FakeWait,
    ended,
    make_report,
    report_text,
)
from tests.support.routing import REFUSED
from tests.support.run_spies import RUN_ID
from tests.unit.test_cli_failure_object import run_command

ID = RUN_ID.value
START = ["start", "--repo", "/work/example", "--task", "add a function"]
RUN = ["run", "--repo", "/work/example", "--task", "add a function"]
COMMANDS = (
    START,
    RUN,
    ["status", ID],
    ["wait", ID],
    ["stop", ID, "--reason", "enough"],
    ["report", ID],
    ["quarantine", "resolve", ID],
)


def book_for(reason: TerminalReason) -> Book:
    """Return a book whose every service answers, and whose run ended with `reason`."""
    text = report_text(make_report(reason))
    return Book(
        wait=FakeWait([ended(reason)] * 3),
        report=FakeReport(result=ReportResult(report_json=text, pending=None)),
    )


class JsonIsTheDefaultTests(unittest.TestCase):
    """No flag, `--json` and `--format json` give the same bytes and the same exit code."""

    def test_the_three_spellings_agree_on_every_command(self) -> None:
        """The flag follows the command name."""
        for argv in COMMANDS:
            head = argv[: 2 if argv[0] == "quarantine" else 1]
            tail = argv[len(head) :]
            plain = run_command(argv, book_for(TerminalReason.COMPLETED))
            for flag in (["--json"], ["--format", "json"]):
                with self.subTest(command=argv[0], flag=flag):
                    flagged = run_command([*head, *flag, *tail], book_for(TerminalReason.COMPLETED))
                    self.assertEqual(flagged, plain, msg="the same code, stdout and stderr")


class TextFormatTests(unittest.TestCase):
    """`--format text` shows the same object, for a person."""

    def test_start_prints_the_text_of_its_object(self) -> None:
        """Literal text of the started run."""
        outcome = run_command([START[0], "--format", "text", *START[1:]], Book())
        self.assertEqual(outcome.code, 0, msg="the exit code is the same")
        self.assertEqual(
            outcome.stdout,
            f"run {ID} is STARTING\nrun_id: {ID}\nstate: STARTING\n",
            msg="text, not JSON",
        )

    def test_report_prints_the_text_of_the_stored_report_and_keeps_its_exit_code(self) -> None:
        """The stored object is rendered; the exit code still says how the run ended."""
        text = report_text(make_report(TerminalReason.FAILED))
        book = Book(report=FakeReport(result=ReportResult(report_json=text, pending=None)))
        outcome = run_command(["report", "--format", "text", ID], book)
        self.assertEqual(outcome.code, 0, msg="report exits 0 for any ended run")
        self.assertEqual(outcome.stdout, render_text(json.loads(text)), msg="the text form")

    def test_run_prints_the_text_of_the_report_and_keeps_its_exit_code(self) -> None:
        """A run that did not complete: text on stdout, the same exit code and stderr line."""
        reason = TerminalReason.FAILED
        expected = run_command(RUN, book_for(reason))
        outcome = run_command([RUN[0], "--format", "text", *RUN[1:]], book_for(reason))
        stored = json.loads(report_text(make_report(reason)))
        self.assertEqual(outcome.code, expected.code, msg="the exit code is the same")
        self.assertEqual(outcome.stderr, expected.stderr, msg="the stderr line is the same")
        self.assertEqual(outcome.stdout, render_text(stored), msg="the text form")


class ConflictTests(unittest.TestCase):
    """The last of `--json` and `--format` wins."""

    def test_the_last_flag_wins_in_every_order(self) -> None:
        """Four conflicts, and the same flag twice."""
        for flags, text in (
            (["--json", "--format", "text"], True),
            (["--format", "text", "--json"], False),
            (["--format", "json", "--format", "text"], True),
            (["--format", "text", "--format", "json"], False),
        ):
            with self.subTest(flags=flags):
                outcome = run_command(["status", *flags, ID], Book())
                self.assertEqual(outcome.stdout.startswith("{"), not text, msg="JSON or text")

    def test_an_unknown_format_is_a_usage_error_with_the_choices(self) -> None:
        """Exit 2; the failure is JSON because the format is not known."""
        outcome = run_command(["status", "--format", "xml", ID], Book())
        self.assertEqual(outcome.code, 2, msg="a usage error")
        self.assertIn("json", json.loads(outcome.stdout)["error"]["message"], msg="the choices")

    def test_the_flag_is_not_accepted_before_the_command(self) -> None:
        """`agentsd --format text status ID` is a usage error."""
        outcome = run_command(["--format", "text", "status", ID], Book())
        self.assertEqual(outcome.code, 2, msg="a usage error")


class FailureFormatTests(unittest.TestCase):
    """A failure after parsing is shown in the chosen format; stderr and the exit stay."""

    def test_a_raised_failure_is_text_in_text_mode(self) -> None:
        """The same code and message, as lines."""
        error = UnknownRunError("no such run")
        failure = describe(error)
        book = Book(status=FakeStatus(error=error))
        outcome = run_command(["status", "--format", "text", ID], book)
        self.assertEqual(outcome.code, 21, msg="the exit code is the same")
        self.assertEqual(
            outcome.stdout,
            f"error {failure.code}\ncode: {failure.code}\nmessage: {failure.text}\n",
            msg="the failure as text",
        )
        self.assertEqual(
            outcome.stderr, f"agentsd: error: {failure.text}\n", msg="stderr is unchanged"
        )

    def test_a_refused_stop_reason_is_text_in_text_mode(self) -> None:
        """A `ValueError` of a use case."""
        book = Book(stop=FakeStop(error=ValueError("reason must not be blank")))
        outcome = run_command(["stop", ID, "--reason", "x", "--format", "text"], book)
        self.assertEqual(outcome.code, 2, msg="invalid input")
        self.assertTrue(outcome.stdout.startswith("error INVALID_ARGUMENT\n"), msg="text failure")

    def test_a_rejection_is_text_with_its_links_in_text_mode(self) -> None:
        """The failure and the rejection beside it."""
        error = NoProviderEligibleError(Rejected((REFUSED,), task_type="audit"))
        book = Book(start=FakeStart(error=error))
        outcome = run_command([START[0], "--format", "text", *START[1:]], book)
        self.assertEqual(outcome.code, 20, msg="a rejection exits 20")
        self.assertTrue(
            outcome.stdout.startswith("error NO_PROVIDER_ELIGIBLE\n"), msg="the failure first"
        )
        self.assertIn("rejection:\n", outcome.stdout, msg="the rejection beside it")

    def test_a_usage_error_is_json_whatever_the_flags_say(self) -> None:
        """The format is not known when the arguments did not parse."""
        outcome = run_command(["wait", ID, "--timeout-s", "0", "--format", "text"], Book())
        self.assertEqual(outcome.code, 2, msg="a usage error")
        self.assertEqual(
            json.loads(outcome.stdout)["error"]["code"], "INVALID_ARGUMENT", msg="JSON"
        )


if __name__ == "__main__":
    unittest.main()
