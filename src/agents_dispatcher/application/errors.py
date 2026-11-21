"""Errors that cross the ports of the run use case."""

from agents_dispatcher.domain.routing import NO_PROVIDER_MESSAGE, Rejected
from agents_dispatcher.domain.run import RunId


class WorkspaceNotGitError(Exception):
    """The repository is not a Git working tree; nothing was created (WORKSPACE_NOT_GIT)."""


class InputTooLargeError(Exception):
    """The task text is longer than `security.input_bytes`; nothing was created (exit 2)."""


class WorkspaceError(Exception):
    """A Git operation of the run workspace failed; the message carries Git's own reason."""


class RunStoreError(Exception):
    """The run store refused a call or could not keep its durability promise."""


class CapacityContendedError(Exception):
    """The provider already holds its capacity of non-terminal runs; nothing was stored."""

    def __init__(self, provider: str) -> None:
        """Keep the provider so `start` can route around it."""
        super().__init__(f"provider {provider} holds its capacity of non-terminal runs")
        self.provider = provider


class LaunchError(Exception):
    """The worker process could not be started or was lost at launch (LAUNCH_LOST)."""


class UnknownRunError(Exception):
    """No run has this identifier (exit 21)."""


class ReportUnavailableError(Exception):
    """The run ended but no report was stored for it (exit 25)."""


class StartInterruptedError(Exception):
    """The caller was interrupted while the supervisor was taking a run over (exit 3).

    The run was accepted and is not closed here: its supervisor may still claim it.
    """

    def __init__(self, run_id: RunId, message: str) -> None:
        """Keep the run id so the caller can still follow or stop the run."""
        super().__init__(message)
        self.run_id = run_id


class StartCancelledError(Exception):
    """The caller gave up before the run was committed; nothing was created."""


class RunNotClaimableError(Exception):
    """The run is no longer waiting for a supervisor: `start` already ended it."""


class LaunchLostError(Exception):
    """The supervisor did not take over the run; the run was ended as LAUNCH_LOST (exit 17)."""

    def __init__(self, run_id: RunId, message: str) -> None:
        """Keep the run id so the caller can still ask for the run's report."""
        super().__init__(message)
        self.run_id = run_id


class InvalidRouteRequestError(Exception):
    """The caller asked for a route the task cannot have; nothing was created (exit 2)."""


class UnknownTaskTypeError(Exception):
    """No settings declare this task type; nothing was created (exit 2)."""


class WriteTaskNotAllowedError(Exception):
    """A write task type is not in `security.allow_write_tasks`; nothing was created (exit 24)."""


class StorageFullError(Exception):
    """The free space of the run area's filesystem is below the configured floor."""


class DecisionLogError(Exception):
    """The SDK log cannot take a decision line, so nothing was created (exit 1)."""


class NoProviderEligibleError(Exception):
    """Every link refused and the chain has no handoff; nothing was created (exit 20)."""

    def __init__(self, rejected: Rejected) -> None:
        """Keep the rejection so the caller can show every refusal."""
        super().__init__(NO_PROVIDER_MESSAGE)
        self.rejected = rejected
