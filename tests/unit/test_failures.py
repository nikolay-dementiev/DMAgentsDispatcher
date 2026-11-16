"""Tests of the failure table: which exception is which kind, problem code and message."""

import unittest

from agents_dispatcher.application.errors import (
    DecisionLogError,
    InputTooLargeError,
    InvalidRouteRequestError,
    LaunchLostError,
    ReportUnavailableError,
    RunStoreError,
    StartInterruptedError,
    UnknownRunError,
    UnknownTaskTypeError,
    WorkspaceError,
    WorkspaceNotGitError,
    WriteTaskNotAllowedError,
)
from agents_dispatcher.application.failures import FailureKind, describe
from agents_dispatcher.application.settings import InvalidSettingsError, SettingsError
from tests.support.run_spies import RUN_ID

TABLE: tuple[tuple[Exception, FailureKind, str, str], ...] = (
    (
        WorkspaceNotGitError("x"),
        FailureKind.INVALID_INPUT,
        "WORKSPACE_NOT_GIT",
        "WORKSPACE_NOT_GIT: x",
    ),
    (InputTooLargeError("x"), FailureKind.INVALID_INPUT, "INPUT_TOO_LARGE", "x"),
    (UnknownTaskTypeError("x"), FailureKind.INVALID_INPUT, "UNKNOWN_TASK_TYPE", "x"),
    (InvalidRouteRequestError("x"), FailureKind.INVALID_INPUT, "INVALID_ROUTE_REQUEST", "x"),
    (
        WriteTaskNotAllowedError("x"),
        FailureKind.AUTHORITY_VIOLATION,
        "AUTHORITY_VIOLATION",
        "AUTHORITY_VIOLATION: x",
    ),
    (DecisionLogError("x"), FailureKind.FAILURE, "DECISION_LOG_ERROR", "x"),
    (UnknownRunError("x"), FailureKind.UNKNOWN_RUN, "UNKNOWN_RUN", "x"),
    (ReportUnavailableError("x"), FailureKind.REPORT_UNAVAILABLE, "REPORT_UNAVAILABLE", "x"),
    (InvalidSettingsError("x"), FailureKind.INVALID_INPUT, "INVALID_SETTINGS", "x"),
    (SettingsError("x"), FailureKind.FAILURE, "SETTINGS_ERROR", "x"),
    (WorkspaceError("x"), FailureKind.FAILURE, "WORKSPACE_ERROR", "x"),
    (RunStoreError("x"), FailureKind.FAILURE, "RUN_STORE_ERROR", "x"),
    (
        LaunchLostError(RUN_ID, "x"),
        FailureKind.LAUNCH_LOST,
        "LAUNCH_LOST",
        f"run {RUN_ID.value} LAUNCH_LOST: x",
    ),
    (RuntimeError("x"), FailureKind.FAILURE, "FAILURE", "unexpected RuntimeError: x"),
)


class FailureTableTests(unittest.TestCase):
    """The table says what the command line printed and what a tool will carry."""

    def test_every_known_failure_has_its_kind_code_and_text(self) -> None:
        """One row per exception; the text is the line the command line printed."""
        for error, kind, code, text in TABLE:
            failure = describe(error)
            self.assertEqual(failure.kind, kind, msg=f"{type(error).__name__} kind")
            self.assertEqual(failure.code, code, msg=f"{type(error).__name__} code")
            self.assertEqual(failure.text, text, msg=f"{type(error).__name__} text")

    def test_an_interrupted_start_names_the_run_and_how_to_follow_it(self) -> None:
        """The caller must learn the run id; the kind is pending, not a failure."""
        failure = describe(StartInterruptedError(RUN_ID, "ignored"))
        self.assertEqual(failure.kind, FailureKind.ACCEPTED_PENDING, msg="kind")
        self.assertEqual(failure.code, "START_INTERRUPTED", msg="code")
        self.assertIn(RUN_ID.value, failure.text, msg="the run id")
        self.assertIn("agentsd wait", failure.text, msg="how to follow it")

    def test_a_subclass_row_wins_over_its_base(self) -> None:
        """An invalid settings file is invalid input; a plain settings error is a failure."""
        self.assertEqual(
            describe(InvalidSettingsError("x")).kind, FailureKind.INVALID_INPUT, msg="sub"
        )
        self.assertEqual(describe(SettingsError("x")).kind, FailureKind.FAILURE, msg="base")


if __name__ == "__main__":
    unittest.main()
