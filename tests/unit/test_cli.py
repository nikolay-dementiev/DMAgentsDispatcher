"""Unit tests of the agentsd command line behaviour."""

import io
import unittest
from collections.abc import Sequence
from dataclasses import dataclass
from typing import NoReturn

from agents_dispatcher.interfaces.cli.app import main
from tests.support.cli_book import Scope
from tests.support.cli_exit_check import assert_listed
from tests.support.cli_failure import failure_of


@dataclass(frozen=True)
class FakeVersionSource:
    """A version source that returns a fixed value, or None when unavailable."""

    version: str | None

    def installed_version(self) -> str | None:
        """Return the fixed version."""
        return self.version


@dataclass(frozen=True)
class Outcome:
    """Everything a command run produced."""

    code: int
    stdout: str
    stderr: str


def unused_service() -> NoReturn:
    """Fail the test: these commands must not build the services."""
    message = "the services were built by a command that does not call a use case"
    raise AssertionError(message)


def run(argv: Sequence[str], version: str | None = "9.9.9-test") -> Outcome:
    """Run the command with in-memory streams and a fake version source."""
    stdout, stderr = io.StringIO(), io.StringIO()
    code = main(argv, stdout, stderr, FakeVersionSource(version), unused_service, Scope())
    assert_listed(argv, code)
    return Outcome(code, stdout.getvalue(), stderr.getvalue())


class VersionTests(unittest.TestCase):
    """The --version option reports what the version source reports."""

    def test_version_prints_the_version_from_the_source(self) -> None:
        """The printed version comes from the injected source, not from a constant."""
        outcome = run(["--version"])
        self.assertEqual(outcome.code, 0, msg="--version succeeds")
        self.assertEqual(outcome.stdout, "agentsd 9.9.9-test\n", msg="version line on stdout")
        self.assertEqual(outcome.stderr, "", msg="nothing on stderr")

    def test_missing_version_metadata_fails_with_an_actionable_message(self) -> None:
        """Without installed metadata the command fails visibly, with no default version."""
        outcome = run(["--version"], version=None)
        self.assertEqual(outcome.code, 1, msg="a missing version is a failure")
        self.assertEqual(
            failure_of(outcome.stdout)["code"], "VERSION_UNKNOWN", msg="no version is invented"
        )
        self.assertIn("not installed", outcome.stderr, msg="the message says what is wrong")
        self.assertNotIn("Traceback", outcome.stderr, msg="no stack trace as the message")


class HelpAndUsageTests(unittest.TestCase):
    """Help and invalid input."""

    def test_help_describes_usage(self) -> None:
        """--help prints usage on stdout and succeeds."""
        outcome = run(["--help"])
        self.assertEqual(outcome.code, 0, msg="--help succeeds")
        self.assertIn("usage: agentsd", outcome.stdout, msg="usage line on stdout")
        self.assertEqual(outcome.stderr, "", msg="nothing on stderr")

    def test_unknown_option_exits_two_with_a_message_on_stderr(self) -> None:
        """An unknown option is invalid input."""
        outcome = run(["--bogus"])
        self.assertEqual(outcome.code, 2, msg="invalid input exits 2")
        self.assertEqual(
            failure_of(outcome.stdout)["code"], "INVALID_ARGUMENT", msg="only a failure object"
        )
        self.assertIn("unrecognized arguments: --bogus", outcome.stderr, msg="names the option")

    def test_no_arguments_exits_two_and_points_to_help(self) -> None:
        """Without a request the command says how to get help."""
        outcome = run([])
        self.assertEqual(outcome.code, 2, msg="no request is invalid input")
        self.assertIn("--help", outcome.stderr, msg="the message points to --help")
