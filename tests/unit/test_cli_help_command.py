"""`agentsd help [COMMAND ...]` prints what `agentsd COMMAND --help` prints.

Owner decision 2026-11-22 (KICKOFF-13): an agent that reaches for `help` as a word finds the same
text as `--help`, on standard output, exit 0, before any service is built.
"""

import unittest

from agents_dispatcher.interfaces.cli.app import build_parser, command_parsers
from tests.support.cli_book import Book
from tests.support.cli_failure import failure_of
from tests.unit.test_cli_failure_object import run_command

PATHS = tuple(command_parsers(build_parser()))


def label(path: tuple[str, ...]) -> str:
    """Return the command as a person types it."""
    return " ".join(path) or "(root)"


class SameAsHelpFlagTests(unittest.TestCase):
    """For every command, `help` and `--help` print the same bytes."""

    def test_help_of_every_path_equals_its_help_flag(self) -> None:
        """The root included; exit 0, stderr empty, no service built."""
        self.assertIn(("help",), PATHS, msg="help is a command of the tree")
        for path in PATHS:
            with self.subTest(command=label(path)):
                book = Book()
                topic = run_command(["help", *path], book)
                flag = run_command([*path, "--help"], Book())
                self.assertEqual(topic.code, 0, msg="a help request succeeds")
                self.assertEqual(topic.stdout, flag.stdout, msg="the same text as --help")
                self.assertEqual(topic.stderr, "", msg="nothing on stderr")
                self.assertEqual(book.factory_calls, 0, msg="no service was built")

    def test_help_of_help_equals_its_flags(self) -> None:
        """`help help`, `help --help` and `help -h` agree."""
        expected = run_command(["help", "help"], Book())
        for argv in (["help", "--help"], ["help", "-h"]):
            with self.subTest(argv=argv):
                outcome = run_command(argv, Book())
                self.assertEqual(outcome.code, 0, msg="a help request succeeds")
                self.assertEqual(outcome.stdout, expected.stdout, msg="the help of help")


class RefusedTopicTests(unittest.TestCase):
    """What `help` does not answer."""

    def test_an_unknown_topic_is_a_usage_error(self) -> None:
        """Exit 2 and the failure object, nothing built."""
        for argv in (["help", "nosuch"], ["help", "start", "extra"], ["help", "start", "--bogus"]):
            with self.subTest(argv=argv):
                book = Book()
                outcome = run_command(argv, book)
                self.assertEqual(outcome.code, 2, msg="a usage error")
                self.assertEqual(
                    failure_of(outcome.stdout)["code"], "INVALID_ARGUMENT", msg="the code"
                )
                self.assertEqual(book.factory_calls, 0, msg="no service was built")

    def test_help_is_not_an_argument_of_another_command(self) -> None:
        """`start help` is an unrecognized argument, as before."""
        outcome = run_command(["start", "help"], Book())
        self.assertEqual(outcome.code, 2, msg="a usage error")


if __name__ == "__main__":
    unittest.main()
