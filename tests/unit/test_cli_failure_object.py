"""A failing command prints the MCP failure object on stdout; stderr and the exit code stay.

Owner decision 2026-11-22: a failure prints the MCP failure object on standard output. The rows
below are the failures that happen before any service is built; each stderr text is what the
command wrote before the change, byte for byte.
"""

import io
import json
import unittest
from dataclasses import dataclass

from agents_dispatcher.application.errors import (
    LaunchLostError,
    QuarantineStateError,
    ReleaseNotConfirmedError,
    StartCancelledError,
    StartInterruptedError,
    UnknownRunError,
    WorkspaceNotGitError,
)
from agents_dispatcher.application.failures import describe
from agents_dispatcher.application.run_queries import ReportResult
from agents_dispatcher.application.tool_schemas import INVALID_ARGUMENT
from agents_dispatcher.domain.views import escape_controls
from agents_dispatcher.interfaces.cli.app import main
from agents_dispatcher.interfaces.cli.exit_codes import EXIT_BY_KIND
from agents_dispatcher.interfaces.cli.failure_codes import ALL_CODES
from tests.support.cli_book import (
    Book,
    FakeReport,
    FakeStart,
    FakeStatus,
    FakeStop,
    FixedVersion,
)
from tests.support.cli_failure import failure_of
from tests.support.run_spies import RUN_ID
from tests.unit.test_failures import TABLE

ID = RUN_ID.value
ROOT_USAGE = "usage: agentsd [-h] [--version] command ...\n"
STATUS_USAGE = "usage: agentsd status [-h] [--format {json,text}] [--json] run_id\n"
WAIT_USAGE = (
    "usage: agentsd wait [-h] [--format {json,text}] [--json]\n"
    "                    [--timeout-s TIMEOUT_S]\n"
    "                    run_id\n"
)
QUARANTINE_USAGE = "usage: agentsd quarantine [-h] resolve ...\n"
BOGUS_COMMAND = (
    "argument command: invalid choice: 'bogus' "
    "(choose from start, run, status, wait, stop, report, quarantine, mcp)"
)
BAD_RUN_ID = "argument run_id: run id must be a canonical lowercase UUIDv4, got 'zzz'"
NO_METADATA = (
    "the package metadata is not installed, so the version is unknown; reinstall the package"
)


class NoVersion:
    """A version source for a package whose metadata is not installed."""

    def installed_version(self) -> str | None:
        """Say that the version is unknown."""
        return None


# argv, version known, exit code, failure code, message, stderr text
ROWS: tuple[tuple[list[str], bool, int, str, str, str], ...] = (
    (
        [],
        True,
        2,
        INVALID_ARGUMENT,
        "nothing to do; try --help",
        f"{ROOT_USAGE}agentsd: error: nothing to do; try --help\n",
    ),
    (
        ["bogus"],
        True,
        2,
        INVALID_ARGUMENT,
        BOGUS_COMMAND,
        f"{ROOT_USAGE}agentsd: error: {BOGUS_COMMAND}\n",
    ),
    (
        ["--bogus"],
        True,
        2,
        INVALID_ARGUMENT,
        "unrecognized arguments: --bogus",
        f"{ROOT_USAGE}agentsd: error: unrecognized arguments: --bogus\n",
    ),
    (
        ["status"],
        True,
        2,
        INVALID_ARGUMENT,
        "the following arguments are required: run_id",
        f"{STATUS_USAGE}agentsd: error: the following arguments are required: run_id\n",
    ),
    (
        ["status", "zzz"],
        True,
        2,
        INVALID_ARGUMENT,
        BAD_RUN_ID,
        f"{STATUS_USAGE}agentsd: error: {BAD_RUN_ID}\n",
    ),
    (
        ["wait", ID, "--timeout-s", "0"],
        True,
        2,
        INVALID_ARGUMENT,
        "argument --timeout-s: must be above zero, got 0",
        f"{WAIT_USAGE}agentsd: error: argument --timeout-s: must be above zero, got 0\n",
    ),
    (
        ["quarantine"],
        True,
        2,
        INVALID_ARGUMENT,
        "quarantine needs a subcommand: resolve",
        f"{QUARANTINE_USAGE}agentsd: error: quarantine needs a subcommand: resolve\n",
    ),
    (
        ["quarantine", "resolve", ID, "--release"],
        True,
        2,
        INVALID_ARGUMENT,
        "--release needs --confirm with the run id repeated",
        "agentsd: error: --release needs --confirm with the run id repeated\n",
    ),
    (
        ["quarantine", "resolve", ID, "--confirm", ID],
        True,
        2,
        INVALID_ARGUMENT,
        "--confirm is only meaningful together with --release",
        "agentsd: error: --confirm is only meaningful together with --release\n",
    ),
    (
        ["mcp"],
        True,
        2,
        INVALID_ARGUMENT,
        "mcp needs a subcommand: serve",
        "usage: agentsd mcp [-h] serve ...\nagentsd: error: mcp needs a subcommand: serve\n",
    ),
    (
        ["mcp", "serve"],
        True,
        1,
        "MCP_UNAVAILABLE",
        "the MCP server is not available in this build",
        "agentsd: error: the MCP server is not available in this build\n",
    ),
    (
        ["--version"],
        False,
        1,
        "VERSION_UNKNOWN",
        NO_METADATA,
        f"agentsd: error: {NO_METADATA}\n",
    ),
)


class FailureObjectTests(unittest.TestCase):
    """Failures that happen before any service is built."""

    def test_each_failure_prints_one_object_and_keeps_stderr_and_the_exit_code(self) -> None:
        """Stdout is exactly the MCP failure object; stderr and the exit code are as before."""
        for argv, known, exit_code, code, message, stderr_text in ROWS:
            with self.subTest(argv=argv):
                book = Book()
                stdout, stderr = io.StringIO(), io.StringIO()
                version = FixedVersion() if known else NoVersion()
                result = main(argv, stdout, stderr, version, book.services, book.scope)
                expected = {"error": {"code": code, "message": message}}
                self.assertEqual(result, exit_code, msg="the exit code is unchanged")
                self.assertEqual(
                    stdout.getvalue(),
                    json.dumps(expected, indent=2) + "\n",
                    msg="one failure object, printed as every other object is",
                )
                self.assertEqual(stderr.getvalue(), stderr_text, msg="stderr is byte for byte")
                self.assertIn(code, ALL_CODES, msg="the code is in the table")
                self.assertEqual(book.factory_calls, 0, msg="no services were built")


@dataclass(frozen=True)
class Result:
    """What one command run printed and returned."""

    code: int
    stdout: str
    stderr: str


def run_command(argv: list[str], book: Book) -> Result:
    """Run a command against the fakes without the catalogue check: any exit code may come out."""
    stdout, stderr = io.StringIO(), io.StringIO()
    code = main(argv, stdout, stderr, FixedVersion(), book.services, book.scope)
    return Result(code, stdout.getvalue(), stderr.getvalue())


RAISED: tuple[Exception, ...] = (
    *(row[0] for row in TABLE if not isinstance(row[0], LaunchLostError)),
    LaunchLostError(RUN_ID, "no acknowledgement in 30 s"),
    QuarantineStateError("not held"),
    ReleaseNotConfirmedError("repeat the run id"),
    StartCancelledError("given up"),
    StartInterruptedError(RUN_ID, "ignored"),
)


class RaisedFailureTests(unittest.TestCase):
    """Failures a use case raises: the object carries what MCP would carry for the same error."""

    def test_every_raised_failure_prints_its_code_and_message_and_keeps_stderr(self) -> None:
        """Code and message are `describe`'s, the exit code is the kind's, stderr is as before."""
        for error in RAISED:
            with self.subTest(error=type(error).__name__):
                failure = describe(error)
                book = Book(status=FakeStatus(error=error))
                outcome = run_command(["status", ID], book)
                expected = {"error": {"code": failure.code, "message": failure.text}}
                self.assertEqual(outcome.code, EXIT_BY_KIND[failure.kind], msg="exit by kind")
                self.assertEqual(
                    outcome.stdout, json.dumps(expected, indent=2) + "\n", msg="one object"
                )
                self.assertEqual(
                    outcome.stderr,
                    f"agentsd: error: {escape_controls(failure.text)}\n",
                    msg="stderr as before",
                )

    def test_a_few_failures_have_their_published_code_and_exit_code(self) -> None:
        """Literal values, so a change of the table cannot pass by agreeing with itself."""
        for error, exit_code, code in (
            (UnknownRunError("no such run"), 21, "UNKNOWN_RUN"),
            (WorkspaceNotGitError("x"), 2, "WORKSPACE_NOT_GIT"),
            (StartInterruptedError(RUN_ID, "ignored"), 3, "START_INTERRUPTED"),
            (RuntimeError("boom"), 1, "FAILURE"),
        ):
            with self.subTest(code=code):
                outcome = run_command(["status", ID], Book(status=FakeStatus(error=error)))
                self.assertEqual(outcome.code, exit_code, msg="the published exit code")
                self.assertEqual(failure_of(outcome.stdout)["code"], code, msg="the code")

    def test_a_bad_stop_reason_is_an_invalid_argument(self) -> None:
        """A `ValueError` of a use case is what MCP calls an invalid argument."""
        book = Book(stop=FakeStop(error=ValueError("reason must not be blank")))
        outcome = run_command(["stop", ID, "--reason", "x"], book)
        self.assertEqual(outcome.code, 2, msg="invalid input")
        self.assertEqual(
            failure_of(outcome.stdout),
            {"code": INVALID_ARGUMENT, "message": "reason must not be blank"},
            msg="the argument code and the message",
        )
        self.assertEqual(
            outcome.stderr, "agentsd: error: reason must not be blank\n", msg="stderr as before"
        )

    def test_an_interrupt_outside_the_scope_is_reported_as_interrupted(self) -> None:
        """The hint on stderr keeps its wording; stdout carries the code."""
        book = Book(start=FakeStart(error=KeyboardInterrupt()))
        outcome = run_command(["start", "--repo", "r", "--task", "t"], book)
        self.assertEqual(outcome.code, 1, msg="a failure")
        self.assertEqual(failure_of(outcome.stdout)["code"], "INTERRUPTED", msg="the code")
        self.assertEqual(
            outcome.stderr,
            "agentsd: interrupted; if a run was created, find it with 'agentsd status'\n",
            msg="the hint is unchanged",
        )

    def test_a_run_that_ended_without_a_stored_report_has_its_own_code(self) -> None:
        """`run` has nothing to print; the failure names the run."""
        book = Book(report=FakeReport(result=ReportResult(report_json=None, pending=None)))
        outcome = run_command(["run", "--repo", "r", "--task", "t"], book)
        self.assertEqual(outcome.code, 1, msg="a failure")
        failure = failure_of(outcome.stdout)
        self.assertEqual(failure["code"], "NO_STORED_REPORT", msg="the code")
        self.assertIn(ID, failure["message"], msg="the run")
        self.assertEqual(
            outcome.stderr,
            f"agentsd: error: run {ID} ended without a stored report\n",
            msg="stderr as before",
        )


class ServeTests(unittest.TestCase):
    """`mcp serve` owns stdout once the server runs."""

    def test_a_failure_after_the_server_started_writes_nothing_more_to_stdout(self) -> None:
        """Only the protocol's own bytes are on stdout; the failure goes to stderr."""
        stdout, stderr = io.StringIO(), io.StringIO()

        def serve() -> int:
            stdout.write("protocol bytes")
            message = "boom"
            raise RuntimeError(message)

        book = Book()
        code = main(
            ["mcp", "serve"],
            stdout,
            stderr,
            FixedVersion(),
            book.services,
            book.scope,
            serve_mcp=serve,
        )
        self.assertEqual(code, 1, msg="a failure")
        self.assertEqual(stdout.getvalue(), "protocol bytes", msg="no object after the protocol")
        self.assertEqual(
            stderr.getvalue(), "agentsd: error: unexpected RuntimeError: boom\n", msg="stderr"
        )


if __name__ == "__main__":
    unittest.main()
