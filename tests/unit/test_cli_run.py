"""Unit tests of `agentsd run`: start, wait with no limit, then print the stored report."""

import json
import unittest

from agents_dispatcher.application.errors import (
    InputTooLargeError,
    LaunchLostError,
    ReportUnavailableError,
    RunStoreError,
    StartInterruptedError,
    WorkspaceNotGitError,
)
from agents_dispatcher.application.run_queries import ReportResult, WaitResult
from agents_dispatcher.domain.run import LimitKind, TaskSpec, TerminalReason
from tests.support.cli_book import (
    PENDING,
    Book,
    FakeReport,
    FakeStart,
    FakeWait,
    ended,
    invoke,
    make_report,
    report_text,
)
from tests.support.run_spies import RUN_ID

ARGS = ["run", "--repo", "/work/example", "--task", "add a function"]


def book_for(reason: TerminalReason, *, limit: LimitKind | None = None) -> Book:
    """Return a book whose run ends with `reason` and whose stored report says so."""
    text = report_text(make_report(reason, limit=limit))
    return Book(
        wait=FakeWait([ended(reason)]),
        report=FakeReport(result=ReportResult(report_json=text, pending=None)),
    )


class RunSuccessTests(unittest.TestCase):
    """A completed run."""

    def test_the_stored_report_is_printed_and_stderr_stays_empty(self) -> None:
        """`run` prints exactly what `report` would print for the finished run."""
        book = book_for(TerminalReason.COMPLETED)
        outcome = invoke(ARGS, book)
        self.assertEqual(outcome.code, 0, msg="a completed run exits 0")
        self.assertEqual(
            outcome.stdout, f"{book.report.result.report_json}\n", msg="the stored report"
        )
        self.assertEqual(outcome.stderr, "", msg="no message for a clean run")
        self.assertEqual(book.report.asked, [RUN_ID], msg="the report of the started run")

    def test_the_task_and_repository_reach_the_use_case_with_no_overrides(self) -> None:
        """Nothing is overridden unless the caller says so."""
        book = book_for(TerminalReason.COMPLETED)
        invoke(ARGS, book)
        self.assertEqual(
            book.start.specs, [TaskSpec(repo="/work/example", task="add a function")], msg="spec"
        )

    def test_limit_overrides_are_passed_on(self) -> None:
        """`--hard-s` and `--memory-bytes` become per-run overrides."""
        book = book_for(TerminalReason.COMPLETED)
        invoke([*ARGS, "--hard-s", "90.5", "--memory-bytes", "4096"], book)
        self.assertEqual(book.start.specs[0].hard_s, 90.5, msg="hard_s override")
        self.assertEqual(book.start.specs[0].memory_bytes, 4096, msg="memory override")

    def test_it_waits_in_one_second_slices_with_no_overall_limit(self) -> None:
        """A long run keeps being waited for until it ends."""
        book = book_for(TerminalReason.COMPLETED)
        book.wait.steps = [PENDING, PENDING, ended(TerminalReason.COMPLETED)]
        invoke(ARGS, book)
        self.assertEqual([timeout for _, timeout in book.wait.calls], [1.0, 1.0, 1.0], msg="slices")


class ExitCodeTests(unittest.TestCase):
    """The exit code follows the terminal reason."""

    def test_each_terminal_reason_has_its_own_code_and_a_message(self) -> None:
        """A run that did not complete says why on stderr and never exits 0."""
        expected = {
            TerminalReason.FAILED: 10,
            TerminalReason.STOPPED: 13,
            TerminalReason.ORPHANED: 14,
            TerminalReason.RESOURCE_LIMIT: 16,
            TerminalReason.LAUNCH_LOST: 17,
        }
        for reason, code in expected.items():
            with self.subTest(reason=reason.name):
                outcome = invoke(ARGS, book_for(reason))
                self.assertEqual(outcome.code, code, msg=f"exit code of {reason.name}")
                self.assertIn(reason.name, outcome.stderr, msg="the reason is named")
                self.assertIn(RUN_ID.value, outcome.stderr, msg="the run id is named")
                self.assertEqual(
                    json.loads(outcome.stdout)["terminal_reason"],
                    reason.name,
                    msg="the report is still printed",
                )

    def test_a_resource_limit_names_the_limit_in_the_message(self) -> None:
        """The settings key of the limit that was hit is shown."""
        outcome = invoke(ARGS, book_for(TerminalReason.RESOURCE_LIMIT, limit=LimitKind.MEMORY))
        self.assertIn("resources.run.memory_bytes", outcome.stderr, msg="limit key")

    def test_the_worker_text_in_the_message_is_cut_and_flattened(self) -> None:
        """One line on stderr, never the whole worker output."""
        report = make_report(TerminalReason.FAILED)
        long_text = "word\n" * 200
        text = json.dumps({**report.as_dict(), "result_text": long_text})
        book = book_for(TerminalReason.FAILED)
        book.report.result = ReportResult(report_json=text, pending=None)
        outcome = invoke(ARGS, book)
        self.assertEqual(len(outcome.stderr.splitlines()), 1, msg="one line")
        self.assertLess(len(outcome.stderr), 500, msg="short")


class InterruptTests(unittest.TestCase):
    """SIGINT or SIGTERM during `run` asks the run to stop and keeps waiting for its end."""

    def test_an_interrupt_requests_one_stop_and_the_wait_continues(self) -> None:
        """The flag stays raised across several slices; only one request is made."""
        book = book_for(TerminalReason.STOPPED)

        def interrupted() -> WaitResult:
            book.scope.flag.value = True
            return PENDING

        book.wait.steps = [interrupted, PENDING, PENDING, ended(TerminalReason.STOPPED)]
        outcome = invoke(ARGS, book)
        self.assertEqual(book.stop.calls, [(RUN_ID, "interrupted")], msg="one stop request")
        self.assertEqual(len(book.wait.calls), 4, msg="the wait went on to the end")
        self.assertEqual(outcome.code, 13, msg="the run ended STOPPED")
        self.assertEqual(
            (book.scope.entered, book.scope.left), (1, 1), msg="handlers installed and removed"
        )

    def test_a_second_interrupt_ends_the_wait_with_three_and_the_stop_request_stays(self) -> None:
        """The caller can always get its terminal back; the run keeps stopping on its own."""
        book = book_for(TerminalReason.STOPPED)

        def interrupted_once() -> WaitResult:
            book.scope.flag.signals = 1
            return PENDING

        def interrupted_twice() -> WaitResult:
            book.scope.flag.signals = 2
            return PENDING

        book.wait.steps = [interrupted_once, interrupted_twice, ended(TerminalReason.STOPPED)]
        outcome = invoke(ARGS, book)
        self.assertEqual(outcome.code, 3, msg="a second interrupt exits 3 like a pending wait")
        self.assertEqual(book.stop.calls, [(RUN_ID, "interrupted")], msg="the one request stays")
        self.assertEqual(len(book.wait.calls), 2, msg="the waiting ended at the second interrupt")
        self.assertEqual(json.loads(outcome.stdout)["result"], "PENDING", msg="status on stdout")
        self.assertIn(RUN_ID.value, outcome.stderr, msg="the run id is named")
        self.assertIn("keeps stopping", outcome.stderr, msg="and what happens to the run")

    def test_an_interrupt_while_the_run_starts_names_the_run_and_exits_three(self) -> None:
        """No bare traceback: the run was accepted, so the id and the next step are given."""
        book = book_for(TerminalReason.COMPLETED)
        book.start = FakeStart(error=StartInterruptedError(RUN_ID, "interrupted"))
        outcome = invoke(ARGS, book)
        self.assertEqual(outcome.code, 3, msg="the run is still pending")
        self.assertIn(RUN_ID.value, outcome.stderr, msg="the run id is named")
        self.assertIn("agentsd stop", outcome.stderr, msg="the way to end it is named")
        self.assertIn("may still start", outcome.stderr, msg="no claim that it is running")
        self.assertEqual(book.wait.calls, [], msg="no waiting after an interrupted start")

    def test_an_interrupt_outside_the_signal_scope_after_the_start_names_the_run(self) -> None:
        """The run is live, so its id is never lost to a bare interrupt."""
        book = book_for(TerminalReason.COMPLETED)

        def interrupted() -> WaitResult:
            raise KeyboardInterrupt

        book.wait.steps = [interrupted]
        outcome = invoke(ARGS, book)
        self.assertEqual(outcome.code, 3, msg="the run is still pending")
        self.assertIn(RUN_ID.value, outcome.stderr, msg="the run id is named")

    def test_an_interrupt_before_the_run_exists_is_a_message_not_a_traceback(self) -> None:
        """A bare KeyboardInterrupt from the start phase before anything was accepted."""
        book = book_for(TerminalReason.COMPLETED)
        book.start = FakeStart(error=KeyboardInterrupt())
        outcome = invoke(ARGS, book)
        self.assertEqual(outcome.code, 1, msg="a failure exit")
        self.assertIn("interrupted", outcome.stderr, msg="says what happened")
        self.assertIn("agentsd status", outcome.stderr, msg="and where to look for a run")

    def test_no_interrupt_means_no_stop_request(self) -> None:
        """A quiet run is never stopped."""
        book = book_for(TerminalReason.COMPLETED)
        invoke(ARGS, book)
        self.assertEqual(book.stop.calls, [], msg="nothing was requested")


class FailureTests(unittest.TestCase):
    """Errors before, during and after the run end visibly and non-zero."""

    def test_a_missing_flag_or_bad_value_is_invalid_input_and_starts_nothing(self) -> None:
        """The services are not even built."""
        cases = (
            ["run", "--repo", "/x"],
            ["run", "--task", "t"],
            ["run"],
            [*ARGS, "--hard-s", "soon"],
            [*ARGS, "--hard-s", "0"],
            [*ARGS, "--memory-bytes", "-1"],
            ["run", "--repo", "/x", "--task", "  "],
        )
        for argv in cases:
            with self.subTest(argv=argv):
                book = book_for(TerminalReason.COMPLETED)
                outcome = invoke(argv, book)
                self.assertEqual(outcome.code, 2, msg="invalid input exits 2")
                self.assertEqual(book.start.specs, [], msg="no run was started")

    def test_a_task_over_the_input_bound_exits_two_and_names_the_bound(self) -> None:
        """Invalid input: nothing was created."""
        book = Book(
            start=FakeStart(error=InputTooLargeError("the task is 11 bytes; the bound is 10"))
        )
        outcome = invoke(ARGS, book)
        self.assertEqual(outcome.code, 2, msg="invalid input exits 2")
        self.assertIn("the bound is 10", outcome.stderr, msg="the bound is named")

    def test_a_task_that_is_not_valid_text_exits_two_with_a_message_and_starts_nothing(
        self,
    ) -> None:
        """Argv bytes that are not UTF-8 arrive as lone surrogates and cannot be stored."""
        book = book_for(TerminalReason.COMPLETED)
        outcome = invoke(["run", "--repo", "/work/example", "--task", "bad \udc80 byte"], book)
        self.assertEqual(outcome.code, 2, msg="invalid input exits 2")
        self.assertIn("UTF-8", outcome.stderr, msg="the cause is named")
        self.assertEqual(book.start.specs, [], msg="no run was started")

    def test_a_directory_that_is_not_a_git_tree_exits_two_and_waits_for_nothing(self) -> None:
        """WORKSPACE_NOT_GIT is a usage error and says so."""
        book = book_for(TerminalReason.COMPLETED)
        book.start = FakeStart(error=WorkspaceNotGitError("not a work tree"))
        outcome = invoke(ARGS, book)
        self.assertEqual(outcome.code, 2, msg="WORKSPACE_NOT_GIT exits 2")
        self.assertIn("WORKSPACE_NOT_GIT", outcome.stderr, msg="the code is named")
        self.assertEqual(book.wait.calls, [], msg="no wait")

    def test_a_supervisor_that_never_took_over_exits_seventeen_without_waiting(self) -> None:
        """The run is already closed LAUNCH_LOST."""
        book = book_for(TerminalReason.COMPLETED)
        book.start = FakeStart(error=LaunchLostError(RUN_ID, "no acknowledgement in 30 s"))
        outcome = invoke(ARGS, book)
        self.assertEqual(outcome.code, 17, msg="LAUNCH_LOST exits 17")
        self.assertEqual(book.wait.calls, [], msg="no wait")

    def test_a_failure_while_waiting_exits_one_with_its_text(self) -> None:
        """Nothing is swallowed and no bare stack trace is printed."""
        book = book_for(TerminalReason.COMPLETED)
        book.wait.steps = [RunStoreError("database is locked")]
        outcome = invoke(ARGS, book)
        self.assertEqual(outcome.code, 1, msg="a failure exits 1")
        self.assertIn("database is locked", outcome.stderr, msg="the reason is shown")
        self.assertNotIn("Traceback", outcome.stderr, msg="no bare trace")

    def test_a_missing_stored_report_exits_twenty_five_after_the_run_ended(self) -> None:
        """The run ended; only its report is gone."""
        book = book_for(TerminalReason.COMPLETED)
        book.report.error = ReportUnavailableError("no report was stored")
        outcome = invoke(ARGS, book)
        self.assertEqual(outcome.code, 25, msg="report unavailable exits 25")


if __name__ == "__main__":
    unittest.main()
