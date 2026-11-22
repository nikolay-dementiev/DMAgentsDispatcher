"""Every command answers `-h` and `--help` on stdout with exit 0, before anything is built."""

import os
import unittest
from unittest import mock

from tests.support.cli_book import Book, invoke

COMMANDS = (
    ("start",),
    ("run",),
    ("status",),
    ("wait",),
    ("stop",),
    ("report",),
    ("quarantine",),
    ("quarantine", "resolve"),
    ("mcp",),
    ("mcp", "serve"),
)
RUN_ID = "0b1c2d3e-4f5a-4b6c-8d7e-9f0a1b2c3d4e"


def label(path: tuple[str, ...]) -> str:
    """Return the command as a person types it."""
    return " ".join(path)


class HelpAnswerTests(unittest.TestCase):
    """What a help request prints, where, and what it leaves untouched."""

    def test_every_command_answers_both_help_flags_with_its_own_usage(self) -> None:
        """Exit 0, `usage: agentsd <command>` on stdout, nothing on stderr, no services built."""
        for path in COMMANDS:
            for flag in ("-h", "--help"):
                with self.subTest(command=label(path), flag=flag):
                    book = Book()
                    outcome = invoke([*path, flag], book)
                    self.assertEqual(outcome.code, 0, msg="a help request succeeds")
                    self.assertTrue(
                        outcome.stdout.startswith(f"usage: agentsd {label(path)}"),
                        msg="the usage line names the command",
                    )
                    self.assertEqual(outcome.stderr, "", msg="nothing on stderr")
                    self.assertEqual(book.factory_calls, 0, msg="the services were not built")

    def test_help_is_answered_before_the_required_arguments_are_checked(self) -> None:
        """`start --help` needs neither `--repo` nor `--task`; `status --help` needs no run id."""
        for argv in (["start", "--help"], ["run", "--help"], ["status", "--help"]):
            with self.subTest(argv=argv):
                self.assertEqual(invoke(argv, Book()).code, 0, msg="no other argument is needed")

    def test_a_help_request_wins_over_an_unknown_option_of_the_same_command(self) -> None:
        """The command's own parser meets `--help` before it can refuse the stranger."""
        for argv in (["start", "--bogus", "--help"], ["start", "--help", "--bogus"]):
            with self.subTest(argv=argv):
                outcome = invoke(argv, Book())
                self.assertEqual(outcome.code, 0, msg="help is answered")
                self.assertTrue(outcome.stdout.startswith("usage: agentsd start"), msg="its help")
                self.assertEqual(outcome.stderr, "", msg="nothing on stderr")


class OptionValueTests(unittest.TestCase):
    """A value that looks like a help flag is not a help request."""

    def test_a_task_text_written_with_an_equals_sign_is_a_task_text(self) -> None:
        """`--task=--help` starts a task whose text is `--help`."""
        book = Book()
        outcome = invoke(["start", "--repo", "r", "--task=--help"], book)
        self.assertEqual(outcome.code, 0, msg="the task was started")
        self.assertEqual([spec.task for spec in book.start.specs], ["--help"], msg="the text")
        self.assertNotIn("usage:", outcome.stdout, msg="no help was printed")

    def test_a_help_flag_in_place_of_a_value_is_still_a_usage_error(self) -> None:
        """`--task --help` and `--task -h` stay errors: the option has no value."""
        for flag in ("--help", "-h"):
            with self.subTest(flag=flag):
                book = Book()
                outcome = invoke(["start", "--repo", "r", "--task", flag], book)
                self.assertEqual(outcome.code, 2, msg="invalid input")
                self.assertIn("argument --task: expected one argument", outcome.stderr, msg="why")
                self.assertEqual(outcome.stdout, "", msg="no help on stdout")
                self.assertEqual(book.start.specs, [], msg="nothing was started")

    def test_an_abbreviated_help_flag_stays_refused(self) -> None:
        """Options are never abbreviated."""
        outcome = invoke(["start", "--he"], Book())
        self.assertEqual(outcome.code, 2, msg="invalid input")
        self.assertEqual(outcome.stdout, "", msg="no help")


class RootCornerTests(unittest.TestCase):
    """The root keeps its behaviour in the corners a help flag could have changed."""

    def test_a_root_help_or_version_with_something_unknown_stays_an_error(self) -> None:
        """Exit 2 with today's message and nothing on stdout."""
        cases = (
            (["--help", "--bogus"], "unrecognized arguments: --bogus"),
            (["--bogus", "--help"], "unrecognized arguments: --bogus"),
            (["--version", "--bogus"], "unrecognized arguments: --bogus"),
            (["--help", "start"], "the following arguments are required: --repo, --task"),
        )
        for argv, message in cases:
            with self.subTest(argv=argv):
                outcome = invoke(argv, Book())
                self.assertEqual(outcome.code, 2, msg="invalid input")
                self.assertIn(message, outcome.stderr, msg="today's message")
                self.assertEqual(outcome.stdout, "", msg="nothing on stdout")


class ColourTests(unittest.TestCase):
    """Help is plain text for an agent, whatever the environment asks for."""

    def test_no_help_contains_an_escape_sequence_when_colour_is_forced(self) -> None:
        """3.14's argparse colours help from `FORCE_COLOR`; the CLI switches that off."""
        with mock.patch.dict(os.environ, {"FORCE_COLOR": "1", "TERM": "xterm"}):
            for path in ((), *COMMANDS):
                with self.subTest(command=label(path)):
                    outcome = invoke([*path, "--help"], Book())
                    self.assertEqual(outcome.code, 0, msg="help is answered")
                    self.assertNotIn("\x1b", outcome.stdout, msg="no escape sequence")


if __name__ == "__main__":
    unittest.main()
