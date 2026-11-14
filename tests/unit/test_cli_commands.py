"""Unit tests of `agentsd start | status | wait | stop | report`: output, exit codes, interrupts."""

import json
import unittest

from agents_dispatcher.application.errors import (
    InputTooLargeError,
    LaunchLostError,
    ReportUnavailableError,
    RunStoreError,
    UnknownRunError,
    WorkspaceNotGitError,
)
from agents_dispatcher.application.run_queries import ReportResult, WaitResult
from agents_dispatcher.application.settings import SettingsError
from agents_dispatcher.domain.run import RunState, TaskSpec, TerminalReason
from agents_dispatcher.domain.views import StopDisposition, StopInfo
from tests.support.cli_book import (
    PENDING,
    Book,
    FakeReport,
    FakeStart,
    FakeStatus,
    FakeStop,
    FakeWait,
    ended,
    invoke,
    make_report,
    make_status,
    make_summary,
    report_text,
)
from tests.support.clock import FakeClock
from tests.support.run_spies import RUN_ID

START = ["start", "--repo", "/work/example", "--task", "add a function"]
ID = RUN_ID.value
SCAN_S = 1.0


class StartTests(unittest.TestCase):
    """`start` accepts a run and prints its id and state."""

    def test_the_run_id_and_state_are_printed_and_the_exit_is_zero(self) -> None:
        """The caller needs only these two to follow the run."""
        book = Book(start=FakeStart(result=make_summary(RunState.RUNNING)))
        outcome = invoke(START, book)
        self.assertEqual(outcome.code, 0, msg="an accepted run exits 0")
        self.assertEqual(
            json.loads(outcome.stdout), {"run_id": ID, "state": "RUNNING"}, msg="the object"
        )
        self.assertEqual(outcome.stderr, "", msg="no message")

    def test_overrides_reach_the_use_case(self) -> None:
        """`--hard-s` and `--memory-bytes` become per-run overrides."""
        book = Book()
        invoke([*START, "--hard-s", "90.5", "--memory-bytes", "4096"], book)
        self.assertEqual(
            book.start.specs,
            [TaskSpec("/work/example", "add a function", hard_s=90.5, memory_bytes=4096)],
            msg="spec",
        )

    def test_a_supervisor_that_never_took_over_exits_seventeen_and_names_the_run(self) -> None:
        """The run id stays on stderr so the caller can still ask for the report."""
        book = Book(start=FakeStart(error=LaunchLostError(RUN_ID, "no acknowledgement in 30 s")))
        outcome = invoke(START, book)
        self.assertEqual(outcome.code, 17, msg="LAUNCH_LOST exits 17")
        self.assertIn(ID, outcome.stderr, msg="the run id is named")
        self.assertIn("no acknowledgement in 30 s", outcome.stderr, msg="the cause is shown")
        self.assertEqual(outcome.stdout, "", msg="nothing on stdout")

    def test_a_task_over_the_input_bound_exits_two_and_names_the_bound(self) -> None:
        """Invalid input: nothing was created."""
        book = Book(
            start=FakeStart(error=InputTooLargeError("the task is 11 bytes; the bound is 10"))
        )
        outcome = invoke(START, book)
        self.assertEqual(outcome.code, 2, msg="invalid input exits 2")
        self.assertIn("the bound is 10", outcome.stderr, msg="the bound is named")

    def test_a_directory_that_is_not_a_git_tree_exits_two(self) -> None:
        """Nothing was created, and the code is named."""
        book = Book(start=FakeStart(error=WorkspaceNotGitError("not a work tree")))
        outcome = invoke(START, book)
        self.assertEqual(outcome.code, 2, msg="WORKSPACE_NOT_GIT exits 2")
        self.assertIn("WORKSPACE_NOT_GIT", outcome.stderr, msg="the code is named")

    def test_missing_flags_and_bad_values_are_invalid_input_and_start_nothing(self) -> None:
        """The use case never runs and the services are never built."""
        cases = (
            ["start", "--repo", "/x"],
            ["start", "--task", "t"],
            [*START, "--hard-s", "soon"],
            [*START, "--hard-s", "0"],
            [*START, "--memory-bytes", "-1"],
            ["start", "--repo", "/x", "--task", "  "],
        )
        for argv in cases:
            with self.subTest(argv=argv):
                book = Book()
                outcome = invoke(argv, book)
                self.assertEqual(outcome.code, 2, msg="invalid input exits 2")
                self.assertEqual(book.start.specs, [], msg="no run was started")
                self.assertEqual(outcome.stdout, "", msg="no output")

    def test_a_store_failure_and_an_unexpected_error_exit_one_with_their_text(self) -> None:
        """Nothing is swallowed and no bare stack trace is printed."""
        for error in (RunStoreError("database is locked"), OSError("disk gone")):
            with self.subTest(error=type(error).__name__):
                outcome = invoke(START, Book(start=FakeStart(error=error)))
                self.assertEqual(outcome.code, 1, msg="a failure exits 1")
                self.assertIn(str(error), outcome.stderr, msg="the text is shown")
                self.assertNotIn("Traceback", outcome.stderr, msg="no bare trace")


class StatusTests(unittest.TestCase):
    """`status` prints the status view."""

    def test_the_view_is_printed_and_the_exit_is_zero_for_any_state(self) -> None:
        """Even a failed run is a successful question."""
        stop = StopInfo(requested_at=12.5, reason="enough")
        book = Book(status=FakeStatus(result=make_status(RunState.STOPPING, stop=stop)))
        outcome = invoke(["status", ID], book)
        self.assertEqual(outcome.code, 0, msg="status exits 0")
        self.assertEqual(
            json.loads(outcome.stdout), book.status.result.as_dict(), msg="the contract view"
        )
        self.assertEqual(book.status.asked, [RUN_ID], msg="the run id was parsed")

    def test_an_unknown_run_exits_twenty_one(self) -> None:
        """A well-formed id nobody knows."""
        book = Book(status=FakeStatus(error=UnknownRunError(f"no run with id {ID}")))
        outcome = invoke(["status", ID], book)
        self.assertEqual(outcome.code, 21, msg="unknown run exits 21")
        self.assertIn(ID, outcome.stderr, msg="the id is named")

    def test_a_malformed_id_is_invalid_input_and_never_reaches_the_use_case(self) -> None:
        """Path tricks and typos stop at the parser."""
        for bad in ("../../etc", "123", "3F2B8C1E-5D4A-4B7E-9C3A-1F2E3D4C5B6A"):
            with self.subTest(bad=bad):
                book = Book()
                outcome = invoke(["status", bad], book)
                self.assertEqual(outcome.code, 2, msg="invalid input exits 2")
                self.assertEqual(book.status.asked, [], msg="the use case was not asked")


class StopTests(unittest.TestCase):
    """`stop` records a request and prints the receipt."""

    def test_the_receipt_is_printed_and_every_disposition_exits_zero(self) -> None:
        """A request that met a finished run is still an answer, not a failure."""
        for disposition in StopDisposition:
            with self.subTest(disposition=disposition.name):
                book = Book(stop=FakeStop(disposition=disposition))
                outcome = invoke(["stop", ID, "--reason", "enough"], book)
                self.assertEqual(outcome.code, 0, msg="stop exits 0")
                self.assertEqual(
                    json.loads(outcome.stdout)["disposition"],
                    disposition.value,
                    msg="the disposition",
                )
                self.assertEqual(book.stop.calls, [(RUN_ID, "enough")], msg="reason passed on")

    def test_a_refused_reason_is_printed_with_its_controls_escaped(self) -> None:
        """The error echoes caller text; it must not move the cursor."""
        book = Book(stop=FakeStop(error=ValueError("bad reason \x1b[2J")))
        outcome = invoke(["stop", ID, "--reason", "x"], book)
        self.assertNotIn("\x1b", outcome.stderr, msg="no raw ESC on the terminal")
        self.assertIn("\\x1b[2J", outcome.stderr, msg="shown as an escape")

    def test_a_missing_blank_or_oversized_reason_is_invalid_input(self) -> None:
        """The use case decides the bound; its ValueError is invalid input."""
        book = Book(stop=FakeStop(error=ValueError("reason is 600 bytes, over the limit")))
        self.assertEqual(invoke(["stop", ID], book).code, 2, msg="reason is required")
        outcome = invoke(["stop", ID, "--reason", "x"], book)
        self.assertEqual(outcome.code, 2, msg="a refused reason exits 2")
        self.assertIn("over the limit", outcome.stderr, msg="the bound is explained")

    def test_an_unknown_run_exits_twenty_one(self) -> None:
        """Nothing was recorded."""
        book = Book(stop=FakeStop(error=UnknownRunError(f"no run with id {ID}")))
        self.assertEqual(invoke(["stop", ID, "--reason", "x"], book).code, 21, msg="exit 21")


class WaitTests(unittest.TestCase):
    """`wait` follows the run to its end or to the timeout."""

    def test_a_ready_run_exits_with_its_terminal_class(self) -> None:
        """The same mapping `run` uses."""
        expected = {
            TerminalReason.COMPLETED: 0,
            TerminalReason.FAILED: 10,
            TerminalReason.STOPPED: 13,
            TerminalReason.ORPHANED: 14,
            TerminalReason.RESOURCE_LIMIT: 16,
            TerminalReason.LAUNCH_LOST: 17,
        }
        for reason, code in expected.items():
            with self.subTest(reason=reason.name):
                outcome = invoke(["wait", ID], Book(wait=FakeWait([ended(reason)])))
                self.assertEqual(outcome.code, code, msg=f"exit code of {reason.name}")
                body = json.loads(outcome.stdout)
                self.assertEqual(body["result"], "READY", msg="result")
                self.assertEqual(body["status"]["run"]["reason"], reason.name, msg="status")

    def test_a_timeout_is_one_wait_with_a_deadline_that_overshoots_by_at_most_a_scan(self) -> None:
        """Slicing by nominal time lost the work done between slices; one deadline does not."""
        clock = FakeClock()
        work_s = 0.7  # what one look at the store costs beyond the pause

        def spend(timeout_s: float | None) -> None:
            clock.advance((timeout_s or 0.0) + work_s)

        book = Book(wait=FakeWait([PENDING]))
        book.wait.cost = spend
        outcome = invoke(["wait", ID, "--timeout-s", "5"], book)
        self.assertEqual(outcome.code, 3, msg="PENDING exits 3")
        self.assertEqual(json.loads(outcome.stdout)["result"], "PENDING", msg="result")
        self.assertEqual([timeout for _, timeout in book.wait.calls], [5.0], msg="one deadline")
        self.assertLessEqual(clock.now(), 5.0 + SCAN_S, msg="at most one scan past the timeout")

    def test_the_wait_is_given_the_interrupt_flag_to_poll(self) -> None:
        """The use case watches the flag between looks, so Ctrl-C is noticed within a scan."""
        book = Book(wait=FakeWait([PENDING]))
        invoke(["wait", ID, "--timeout-s", "5"], book)
        self.assertEqual(
            book.wait.stop_probes, [book.scope.flag.raised], msg="the flag is the stop probe"
        )

    def test_without_a_timeout_one_wait_covers_the_whole_run(self) -> None:
        """The use case loops until the run ends; the command does not slice."""
        book = Book(wait=FakeWait([ended(TerminalReason.COMPLETED)]))
        outcome = invoke(["wait", ID], book)
        self.assertEqual(outcome.code, 0, msg="the run completed")
        self.assertEqual([timeout for _, timeout in book.wait.calls], [None], msg="no deadline")

    def test_a_non_positive_or_non_numeric_timeout_is_invalid_input(self) -> None:
        """Nothing is waited for."""
        for value in ("0", "-1", "soon"):
            with self.subTest(value=value):
                book = Book()
                self.assertEqual(
                    invoke(["wait", ID, "--timeout-s", value], book).code, 2, msg="exit 2"
                )
                self.assertEqual(book.wait.calls, [], msg="no wait")

    def test_an_unknown_run_exits_twenty_one(self) -> None:
        """The use case raises on the first look."""
        book = Book(wait=FakeWait([UnknownRunError(f"no run with id {ID}")]))
        self.assertEqual(invoke(["wait", ID], book).code, 21, msg="exit 21")

    def test_an_interrupt_ends_the_wait_with_three_and_leaves_the_run_alone(self) -> None:
        """SIGINT on `wait` must not stop the run."""
        book = Book()

        def interrupted() -> WaitResult:
            book.scope.flag.value = True
            return PENDING

        book.wait.steps = [interrupted]
        outcome = invoke(["wait", ID], book)
        self.assertEqual(outcome.code, 3, msg="an interrupted wait exits 3")
        self.assertEqual(book.stop.calls, [], msg="the run was not asked to stop")
        self.assertIn("interrupted", outcome.stderr, msg="the reason is on stderr")
        self.assertEqual(
            (book.scope.entered, book.scope.left), (1, 1), msg="handlers installed and removed"
        )


class ReportTests(unittest.TestCase):
    """`report` prints the stored report text."""

    def test_the_stored_text_is_printed_unchanged_and_the_exit_is_zero(self) -> None:
        """Even for a failed run: the question was answered."""
        text = report_text(make_report(TerminalReason.FAILED))
        book = Book(report=FakeReport(result=ReportResult(report_json=text, pending=None)))
        outcome = invoke(["report", ID], book)
        self.assertEqual(outcome.code, 0, msg="report exits 0")
        self.assertEqual(outcome.stdout, text + "\n", msg="the stored text")

    def test_an_unfinished_run_prints_pending_with_its_status_and_exits_three(self) -> None:
        """The caller should wait first."""
        book = Book(report=FakeReport(result=ReportResult(None, make_status())))
        outcome = invoke(["report", ID], book)
        self.assertEqual(outcome.code, 3, msg="unfinished exits 3")
        body = json.loads(outcome.stdout)
        self.assertEqual(body["result"], "PENDING", msg="result")
        self.assertEqual(body["status"], make_status().as_dict(), msg="status")

    def test_an_unknown_run_exits_twenty_one_and_a_missing_report_twenty_five(self) -> None:
        """Two different problems, two codes."""
        unknown = Book(report=FakeReport(error=UnknownRunError(f"no run with id {ID}")))
        self.assertEqual(invoke(["report", ID], unknown).code, 21, msg="unknown run")
        missing = Book(report=FakeReport(error=ReportUnavailableError("no report was stored")))
        outcome = invoke(["report", ID], missing)
        self.assertEqual(outcome.code, 25, msg="report unavailable")
        self.assertIn("no report was stored", outcome.stderr, msg="the cause is shown")


class LazinessTests(unittest.TestCase):
    """Help, version and parse errors must not load settings or build adapters."""

    def test_version_help_and_usage_errors_do_not_build_the_services(self) -> None:
        """Broken settings must not hide `--help`."""
        for argv, code in ((["--version"], 0), (["--help"], 0), (["status"], 2), ([], 2)):
            with self.subTest(argv=argv):
                book = Book(factory_error=SettingsError("settings: broken"))
                self.assertEqual(invoke(argv, book).code, code, msg="exit code")
                self.assertEqual(book.factory_calls, 0, msg="the factory was not called")

    def test_settings_that_cannot_be_loaded_exit_one_naming_the_key(self) -> None:
        """The factory fails for a real command."""
        book = Book(factory_error=SettingsError("settings: watchdog.hard_s: not a number"))
        outcome = invoke(["status", ID], book)
        self.assertEqual(outcome.code, 1, msg="a settings failure exits 1")
        self.assertIn("watchdog.hard_s", outcome.stderr, msg="the key is shown")


if __name__ == "__main__":
    unittest.main()
