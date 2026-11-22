"""The failures only the command line has, and the set of every code it can print.

A failure of a use case gets its code from `application.failures`; a refusal of the arguments
carries the code MCP uses for the same input. The four failures below belong to the command
itself (it has no version, no MCP server, was interrupted, or a run left no report), so the MCP
tools never show them.
"""

from agents_dispatcher.application.failures import FAILURE_CODES, Failure, FailureKind
from agents_dispatcher.application.tool_schemas import INVALID_ARGUMENT
from agents_dispatcher.domain.run import RunId

VERSION_UNKNOWN = Failure(
    FailureKind.FAILURE,
    "VERSION_UNKNOWN",
    "the package metadata is not installed, so the version is unknown; reinstall the package",
)
MCP_UNAVAILABLE = Failure(
    FailureKind.FAILURE, "MCP_UNAVAILABLE", "the MCP server is not available in this build"
)
INTERRUPTED = Failure(
    FailureKind.FAILURE,
    "INTERRUPTED",
    "interrupted; if a run was created, find it with 'agentsd status'",
)

CLI_CODES = frozenset({"VERSION_UNKNOWN", "MCP_UNAVAILABLE", "INTERRUPTED", "NO_STORED_REPORT"})
ALL_CODES = FAILURE_CODES | CLI_CODES | {INVALID_ARGUMENT}


def no_stored_report(run_id: RunId) -> Failure:
    """Return the failure of a run that ended and left no stored report."""
    return Failure(
        FailureKind.FAILURE,
        "NO_STORED_REPORT",
        f"run {run_id.value} ended without a stored report",
    )


def refusal(message: str) -> Failure:
    """Return the failure of arguments the command refuses, with the code MCP uses for them."""
    return Failure(FailureKind.INVALID_INPUT, INVALID_ARGUMENT, message)
