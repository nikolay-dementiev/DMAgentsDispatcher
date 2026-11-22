"""The failure codes the command line can print: the shared ones, the MCP one and its own four."""

import unittest

from agents_dispatcher.application.failures import FAILURE_CODES, FailureKind
from agents_dispatcher.application.tool_schemas import INVALID_ARGUMENT
from agents_dispatcher.domain.run import RunId
from agents_dispatcher.interfaces.cli.exit_codes import (
    EXIT_BY_KIND,
    EXIT_FAILURE,
    EXIT_INVALID_INPUT,
)
from agents_dispatcher.interfaces.cli.failure_codes import (
    ALL_CODES,
    CLI_CODES,
    INTERRUPTED,
    MCP_UNAVAILABLE,
    VERSION_UNKNOWN,
    no_stored_report,
    refusal,
)

RUN = RunId("0b1c2d3e-4f5a-4b6c-8d7e-9f0a1b2c3d4e")
SHARED = frozenset(
    {
        "WORKSPACE_NOT_GIT",
        "INPUT_TOO_LARGE",
        "UNKNOWN_TASK_TYPE",
        "INVALID_ROUTE_REQUEST",
        "AUTHORITY_VIOLATION",
        "NO_PROVIDER_ELIGIBLE",
        "DECISION_LOG_ERROR",
        "STORAGE_FULL",
        "NOT_QUARANTINED",
        "RELEASE_NOT_CONFIRMED",
        "UNKNOWN_RUN",
        "REPORT_UNAVAILABLE",
        "INVALID_SETTINGS",
        "SETTINGS_ERROR",
        "WORKSPACE_ERROR",
        "RUN_STORE_ERROR",
        "CANCELLED",
        "LAUNCH_LOST",
        "START_INTERRUPTED",
        "FAILURE",
    }
)
OWN = frozenset({"VERSION_UNKNOWN", "MCP_UNAVAILABLE", "INTERRUPTED", "NO_STORED_REPORT"})


class CodeSetTests(unittest.TestCase):
    """Which codes exist; a new or renamed code must be a deliberate edit of this test."""

    def test_the_shared_codes_are_the_table_of_the_use_cases(self) -> None:
        """Every code `describe` can return, and no other."""
        self.assertEqual(FAILURE_CODES, SHARED, msg="the codes of failures.py")

    def test_the_command_line_has_exactly_four_codes_of_its_own(self) -> None:
        """They are named, and none of them is a shared or an MCP code."""
        self.assertEqual(CLI_CODES, OWN, msg="the four codes only the command line prints")
        self.assertFalse(CLI_CODES & FAILURE_CODES, msg="not a shared code")
        self.assertNotIn(INVALID_ARGUMENT, CLI_CODES, msg="not the MCP code")

    def test_every_code_the_command_line_prints_is_in_one_set(self) -> None:
        """The union is what a scenario may see in a failure object."""
        self.assertEqual(
            ALL_CODES, SHARED | OWN | {INVALID_ARGUMENT}, msg="shared, own and the argument code"
        )


class FailureValueTests(unittest.TestCase):
    """The value of each own failure: its kind, so its exit code, its code and its text."""

    def test_the_own_failures_exit_1_with_their_code(self) -> None:
        """A failure of the command itself, not of the input."""
        for failure, code in (
            (VERSION_UNKNOWN, "VERSION_UNKNOWN"),
            (MCP_UNAVAILABLE, "MCP_UNAVAILABLE"),
            (INTERRUPTED, "INTERRUPTED"),
            (no_stored_report(RUN), "NO_STORED_REPORT"),
        ):
            with self.subTest(code=code):
                self.assertEqual(failure.code, code, msg="the code")
                self.assertIs(failure.kind, FailureKind.FAILURE, msg="a failure kind")
                self.assertEqual(EXIT_BY_KIND[failure.kind], EXIT_FAILURE, msg="exit 1")
                self.assertTrue(failure.text, msg="a message")

    def test_the_report_failure_names_the_run(self) -> None:
        """The message of a run that ended without a stored report says which run."""
        self.assertIn(RUN.value, no_stored_report(RUN).text, msg="the run id is in the message")

    def test_a_refusal_is_an_invalid_argument_that_exits_2(self) -> None:
        """Arguments the command refuses carry the MCP code and the message as given."""
        failure = refusal("--timeout-s: must be above zero")
        self.assertEqual(failure.code, INVALID_ARGUMENT, msg="the MCP code")
        self.assertEqual(failure.text, "--timeout-s: must be above zero", msg="the message")
        self.assertEqual(EXIT_BY_KIND[failure.kind], EXIT_INVALID_INPUT, msg="exit 2")


if __name__ == "__main__":
    unittest.main()
