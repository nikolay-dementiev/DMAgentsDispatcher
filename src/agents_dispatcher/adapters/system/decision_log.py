"""The SDK log: one line per routing decision, in a private file next to the run logs.

A decision without a run has no run log, so every decision goes here, before anything is created.
The brief of a task is never written.
"""

import os
import time
from pathlib import Path

from agents_dispatcher.application.errors import DecisionLogError
from agents_dispatcher.domain.routing import (
    Decision,
    Evaluation,
    Recommendation,
    ResolvedTask,
    RouteMode,
    Started,
)

FOLDER_MODE = 0o700
FILE_MODE = 0o600
FILE_NAME = "sdk.log"


def _kind(decision: Decision) -> str:
    """Return the word that names the kind of decision."""
    if isinstance(decision, Started):
        return "STARTED"
    if isinstance(decision, Recommendation):
        return "RECOMMENDATION"
    return "REJECTED"


def _link(evaluation: Evaluation) -> str:
    """Return one evaluated link: its provider, disposition and the codes that refused it."""
    codes = ",".join(problem.code.value for problem in evaluation.problems)
    detail = f"({codes})" if codes else ""
    return f"{evaluation.provider}:{evaluation.disposition.value}{detail}"


def _evaluations(decision: Decision) -> tuple[Evaluation, ...]:
    """Return every link the decision evaluated, in chain order."""
    if isinstance(decision, Started):
        return decision.evaluations
    if isinstance(decision, Recommendation):
        return decision.refusals
    return decision.context


class FileDecisionLog:
    """Appends one fsynced line per decision to `<directory>/sdk.log`, for the owner only."""

    def __init__(self, directory: str) -> None:
        """Remember where the log lives; nothing is created until the first decision."""
        self._directory = Path(directory)

    def record(self, task: ResolvedTask, decision: Decision, mode: RouteMode) -> None:
        """Append the line of `decision`; raise `DecisionLogError` when it cannot be written."""
        links = ",".join(_link(item) for item in _evaluations(decision))
        line = (
            f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} DECISION {_kind(decision)} "
            f"task_type={task.task_type or '-'} mode={task.mode.value} request={mode.value} "
            f"links={links}\n"
        )
        _append(self._directory, line)


class FileInspectionLog:
    """Appends one line per account look to the same `sdk.log`, for the owner only."""

    def __init__(self, directory: str) -> None:
        """Remember where the log lives."""
        self._directory = Path(directory)

    def record(
        self, provider: str, look: str, outcome: str, destinations: tuple[str, ...] = ()
    ) -> None:
        """Append the line of one look; raise `DecisionLogError` when it cannot be written."""
        shown = ",".join(destinations) or "-"
        line = (
            f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} INSPECTION provider={provider} look={look} "
            f"outcome={_one_line(outcome)} destinations={shown}\n"
        )
        _append(self._directory, line)


def _one_line(text: str) -> str:
    """Return `text` on one line, so one look is one log line."""
    return " ".join(text.split())


def _append(directory: Path, line: str) -> None:
    """Append `line` to `<directory>/sdk.log`, creating the private folder and file."""
    path = directory / FILE_NAME
    try:
        directory.mkdir(mode=FOLDER_MODE, parents=True, exist_ok=True)
        directory.chmod(FOLDER_MODE)
        descriptor = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, FILE_MODE)
        try:
            os.write(descriptor, line.encode())
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as error:
        message = f"cannot write the decision to {path}: {error}"
        raise DecisionLogError(message) from error
