"""The exit codes of `agentsd`: one table for the code, the help and the README."""

from dataclasses import dataclass

from agents_dispatcher.application.failures import FailureKind
from agents_dispatcher.domain.run import TerminalReason

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_INVALID_INPUT = 2
EXIT_PENDING = 3
EXIT_REJECTED = 20
EXIT_UNKNOWN_RUN = 21
EXIT_AUTHORITY_VIOLATION = 24
EXIT_REPORT_UNAVAILABLE = 25
# One code per terminal reason, in the architecture's 10-19 range (decision Q2 of PLAN-4).
EXIT_BY_REASON = {
    TerminalReason.COMPLETED: EXIT_OK,
    TerminalReason.FAILED: 10,
    TerminalReason.STOPPED: 13,
    TerminalReason.ORPHANED: 14,
    TerminalReason.RESOURCE_LIMIT: 16,
    TerminalReason.LAUNCH_LOST: 17,
}
EXIT_LAUNCH_LOST = EXIT_BY_REASON[TerminalReason.LAUNCH_LOST]
EXIT_BY_KIND = {
    FailureKind.INVALID_INPUT: EXIT_INVALID_INPUT,
    FailureKind.AUTHORITY_VIOLATION: EXIT_AUTHORITY_VIOLATION,
    FailureKind.FAILURE: EXIT_FAILURE,
    FailureKind.UNKNOWN_RUN: EXIT_UNKNOWN_RUN,
    FailureKind.REPORT_UNAVAILABLE: EXIT_REPORT_UNAVAILABLE,
    FailureKind.REJECTED: EXIT_REJECTED,
    FailureKind.LAUNCH_LOST: EXIT_LAUNCH_LOST,
    FailureKind.ACCEPTED_PENDING: EXIT_PENDING,
}


@dataclass(frozen=True, slots=True)
class ExitCode:
    """One row of the table: the number, what the code calls it and what it means."""

    code: int
    name: str
    meaning: str


EXIT_CODES = (
    ExitCode(
        EXIT_OK,
        "EXIT_OK",
        'completed (`start`, `status`, `stop` and `wait` also use 0 for "done what you asked")',
    ),
    ExitCode(
        EXIT_FAILURE,
        "EXIT_FAILURE",
        "the run could not be set up, an unexpected error occurred, "
        "or `quarantine resolve` settled the run but a warning followed",
    ),
    ExitCode(
        EXIT_INVALID_INPUT,
        "EXIT_INVALID_INPUT",
        "invalid input or settings, an unknown task type, or the directory is not a Git work tree",
    ),
    ExitCode(
        EXIT_PENDING,
        "EXIT_PENDING",
        "pending: `wait` timed out or was interrupted, `report` of an unfinished run, "
        "or a run that is QUARANTINED (`wait`, `agentsd run` and `quarantine resolve` "
        "return at once with it)",
    ),
    ExitCode(10, "FAILED", "the worker failed"),
    ExitCode(13, "STOPPED", "stopped"),
    ExitCode(
        14, "ORPHANED", "orphaned (the supervisor died; `wait` closes the run and stops its worker)"
    ),
    ExitCode(16, "RESOURCE_LIMIT", "a resource limit was hit"),
    ExitCode(EXIT_LAUNCH_LOST, "LAUNCH_LOST", "the worker never started"),
    ExitCode(
        EXIT_REJECTED,
        "EXIT_REJECTED",
        "the submission was rejected: no provider of the chain could take the task",
    ),
    ExitCode(EXIT_UNKNOWN_RUN, "EXIT_UNKNOWN_RUN", "unknown run id"),
    ExitCode(
        EXIT_AUTHORITY_VIOLATION,
        "EXIT_AUTHORITY_VIOLATION",
        "a write task type that `security.allow_write_tasks` does not list",
    ),
    ExitCode(EXIT_REPORT_UNAVAILABLE, "EXIT_REPORT_UNAVAILABLE", "the report is unavailable"),
)


def render_markdown() -> str:
    """Return the table as the Markdown block the README must hold."""
    rows = [f"| {row.code} | {row.meaning} |" for row in EXIT_CODES]
    return "\n".join(["| Exit code | Meaning |", "|---|---|", *rows])
