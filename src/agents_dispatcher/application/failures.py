"""What each failure of a use case means to a caller: its kind, problem code and message.

The command line maps the kind to an exit code; the MCP tools carry the code. One table, so the
two front ends cannot disagree about which failure is which.
"""

from dataclasses import dataclass
from enum import Enum

from agents_dispatcher.application.errors import (
    DecisionLogError,
    InputTooLargeError,
    InvalidRouteRequestError,
    LaunchLostError,
    NoProviderEligibleError,
    QuarantineStateError,
    ReleaseNotConfirmedError,
    ReportUnavailableError,
    RunStoreError,
    StartCancelledError,
    StartInterruptedError,
    StorageFullError,
    UnknownRunError,
    UnknownTaskTypeError,
    WorkspaceError,
    WorkspaceNotGitError,
    WriteTaskNotAllowedError,
)
from agents_dispatcher.application.settings import InvalidSettingsError, SettingsError


class FailureKind(Enum):
    """The class of outcome a front end reports for a failure."""

    INVALID_INPUT = "INVALID_INPUT"
    AUTHORITY_VIOLATION = "AUTHORITY_VIOLATION"
    FAILURE = "FAILURE"
    UNKNOWN_RUN = "UNKNOWN_RUN"
    REPORT_UNAVAILABLE = "REPORT_UNAVAILABLE"
    REJECTED = "REJECTED"
    LAUNCH_LOST = "LAUNCH_LOST"
    ACCEPTED_PENDING = "ACCEPTED_PENDING"


@dataclass(frozen=True)
class Failure:
    """A failure as a caller sees it."""

    kind: FailureKind
    code: str
    text: str


# Order matters: a subclass row comes before its base.
_ROWS: tuple[tuple[type[Exception], FailureKind, str, str], ...] = (
    (WorkspaceNotGitError, FailureKind.INVALID_INPUT, "WORKSPACE_NOT_GIT", "WORKSPACE_NOT_GIT: "),
    (InputTooLargeError, FailureKind.INVALID_INPUT, "INPUT_TOO_LARGE", ""),
    (UnknownTaskTypeError, FailureKind.INVALID_INPUT, "UNKNOWN_TASK_TYPE", ""),
    (InvalidRouteRequestError, FailureKind.INVALID_INPUT, "INVALID_ROUTE_REQUEST", ""),
    (
        WriteTaskNotAllowedError,
        FailureKind.AUTHORITY_VIOLATION,
        "AUTHORITY_VIOLATION",
        "AUTHORITY_VIOLATION: ",
    ),
    (
        NoProviderEligibleError,
        FailureKind.REJECTED,
        "NO_PROVIDER_ELIGIBLE",
        "NO_PROVIDER_ELIGIBLE: ",
    ),
    (DecisionLogError, FailureKind.FAILURE, "DECISION_LOG_ERROR", ""),
    (StorageFullError, FailureKind.FAILURE, "STORAGE_FULL", "STORAGE_FULL: "),
    (QuarantineStateError, FailureKind.INVALID_INPUT, "NOT_QUARANTINED", ""),
    (ReleaseNotConfirmedError, FailureKind.INVALID_INPUT, "RELEASE_NOT_CONFIRMED", ""),
    (UnknownRunError, FailureKind.UNKNOWN_RUN, "UNKNOWN_RUN", ""),
    (ReportUnavailableError, FailureKind.REPORT_UNAVAILABLE, "REPORT_UNAVAILABLE", ""),
    (InvalidSettingsError, FailureKind.INVALID_INPUT, "INVALID_SETTINGS", ""),
    (SettingsError, FailureKind.FAILURE, "SETTINGS_ERROR", ""),
    (WorkspaceError, FailureKind.FAILURE, "WORKSPACE_ERROR", ""),
    (RunStoreError, FailureKind.FAILURE, "RUN_STORE_ERROR", ""),
    (StartCancelledError, FailureKind.FAILURE, "CANCELLED", ""),
)


# Every code `describe` can return: the table rows and the three special cases below.
FAILURE_CODES = frozenset(
    {row[2] for row in _ROWS} | {"LAUNCH_LOST", "START_INTERRUPTED", "FAILURE"}
)


def describe(error: Exception) -> Failure:
    """Return the kind, code and message of a failure."""
    if isinstance(error, LaunchLostError):
        text = f"run {error.run_id.value} LAUNCH_LOST: {error}"
        return Failure(FailureKind.LAUNCH_LOST, "LAUNCH_LOST", text)
    if isinstance(error, StartInterruptedError):
        run = error.run_id.value
        text = (
            f"run {run} was accepted and may still start in the background (it is closed as "
            f"LAUNCH_LOST if no supervisor claims it); follow it with 'agentsd wait {run}' "
            f"or end it with 'agentsd stop {run}'"
        )
        return Failure(FailureKind.ACCEPTED_PENDING, "START_INTERRUPTED", text)
    for kind_of_error, kind, code, prefix in _ROWS:
        if isinstance(error, kind_of_error):
            return Failure(kind, code, f"{prefix}{error}")
    return Failure(FailureKind.FAILURE, "FAILURE", f"unexpected {type(error).__name__}: {error}")
