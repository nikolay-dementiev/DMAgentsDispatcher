"""A usage error shows the usage of the command that failed, then today's message."""

import unittest

from tests.support.cli_book import Book, invoke
from tests.support.run_spies import RUN_ID

ID = RUN_ID.value
FAILING = (
    (["status"], "status", "the following arguments are required: run_id"),
    (["status", "zzz"], "status", "argument run_id: run id must be a canonical lowercase UUIDv4"),
    (["status", ID, "--bogus"], "status", "unrecognized arguments: --bogus"),
    (["wait", ID, "--timeout-s", "0"], "wait", "argument --timeout-s: must be above zero"),
    (["start", "--repo", "r"], "start", "the following arguments are required: --task"),
    (["run", "--repo", "r", "--task", "t", "--bogus"], "run", "unrecognized arguments: --bogus"),
    (["stop", ID], "stop", "the following arguments are required: --reason"),
    (["report"], "report", "the following arguments are required: run_id"),
    (["quarantine"], "quarantine", "quarantine needs a subcommand: resolve"),
    (["quarantine", "resolve"], "quarantine resolve", "the following arguments are required"),
    (
        ["quarantine", "resolve", ID, "--bogus"],
        "quarantine resolve",
        "unrecognized arguments: --bogus",
    ),
    (["mcp"], "mcp", "mcp needs a subcommand: serve"),
    (["mcp", "serve", "--port", "1"], "mcp serve", "unrecognized arguments: --port 1"),
)


class CommandUsageTests(unittest.TestCase):
    """Which usage line leads the message."""

    def test_the_usage_line_names_the_failing_command_and_the_message_is_unchanged(self) -> None:
        """Exit 2, `usage: agentsd <command>` first, then `agentsd: error: <message>`."""
        for argv, command, message in FAILING:
            with self.subTest(argv=argv):
                book = Book()
                outcome = invoke(argv, book)
                self.assertEqual(outcome.code, 2, msg="invalid input")
                self.assertTrue(
                    outcome.stderr.startswith(f"usage: agentsd {command}"),
                    msg=f"the usage of {command}, got: {outcome.stderr.splitlines()[:1]}",
                )
                self.assertIn(f"agentsd: error: {message}", outcome.stderr, msg="today's message")
                self.assertEqual(outcome.stdout, "", msg="nothing on stdout")
                self.assertEqual(book.factory_calls, 0, msg="no services were built")


class ControlCharacterTests(unittest.TestCase):
    """An argument can come from untrusted text; it never reaches the terminal raw."""

    def test_an_unrecognized_argument_with_a_terminal_escape_is_printed_escaped(self) -> None:
        """Both the root and a command refuse it with exit 2 and no raw ESC on stderr."""
        for argv in (["--bogus\x1b[2J"], ["status", ID, "--bogus\x1b[2J"]):
            with self.subTest(argv=argv):
                outcome = invoke(argv, Book())
                self.assertEqual(outcome.code, 2, msg="invalid input")
                self.assertNotIn("\x1b", outcome.stderr, msg="the escape is not written raw")
                self.assertIn("unrecognized arguments: --bogus", outcome.stderr, msg="still told")


class RootUsageTests(unittest.TestCase):
    """What belongs to the root keeps the root's usage."""

    def test_no_command_an_unknown_command_and_an_unknown_root_option_show_the_root_usage(
        self,
    ) -> None:
        """`agentsd`, `agentsd bogus` and `agentsd --bogus` are about the program, not a command."""
        cases: tuple[tuple[list[str], str], ...] = (
            ([], "nothing to do; try --help"),
            (["bogus"], "argument command: invalid choice: 'bogus'"),
            (["--bogus"], "unrecognized arguments: --bogus"),
        )
        for argv, message in cases:
            with self.subTest(argv=argv):
                outcome = invoke(argv, Book())
                self.assertEqual(outcome.code, 2, msg="invalid input")
                self.assertTrue(
                    outcome.stderr.startswith("usage: agentsd [-h]"), msg="the root usage"
                )
                self.assertIn(f"agentsd: error: {message}", outcome.stderr, msg="the message")


if __name__ == "__main__":
    unittest.main()
