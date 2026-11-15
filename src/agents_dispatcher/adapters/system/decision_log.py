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
        path = self._directory / FILE_NAME
        try:
            self._directory.mkdir(mode=FOLDER_MODE, parents=True, exist_ok=True)
            self._directory.chmod(FOLDER_MODE)
            descriptor = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_APPEND, FILE_MODE)
            try:
                os.write(descriptor, line.encode())
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as error:
            message = f"cannot write the decision to {path}: {error}"
            raise DecisionLogError(message) from error
