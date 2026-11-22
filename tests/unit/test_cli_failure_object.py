"""A failing command prints the MCP failure object on stdout; stderr and the exit code stay.

Owner decision 2026-11-22: a failure prints the MCP failure object on standard output. The rows
below are the failures that happen before any service is built; each stderr text is what the
command wrote before the change, byte for byte.
"""

import io
import json
import unittest

from agents_dispatcher.application.tool_schemas import INVALID_ARGUMENT
from agents_dispatcher.interfaces.cli.app import main
from agents_dispatcher.interfaces.cli.failure_codes import ALL_CODES
from tests.support.cli_book import Book, FixedVersion
from tests.support.run_spies import RUN_ID

ID = RUN_ID.value
ROOT_USAGE = "usage: agentsd [-h] [--version] command ...\n"
STATUS_USAGE = "usage: agentsd status [-h] run_id\n"
WAIT_USAGE = "usage: agentsd wait [-h] [--timeout-s TIMEOUT_S] run_id\n"
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


if __name__ == "__main__":
    unittest.main()
