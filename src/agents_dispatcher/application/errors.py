"""Errors that cross the ports of the run use case."""


class WorkspaceNotGitError(Exception):
    """The repository is not a Git working tree; nothing was created (WORKSPACE_NOT_GIT)."""


class WorkspaceError(Exception):
    """A Git operation of the run workspace failed; the message carries Git's own reason."""


class RunStoreError(Exception):
    """The run store refused a call or could not keep its durability promise."""


class LaunchError(Exception):
    """The worker process could not be started or was lost at launch (LAUNCH_LOST)."""
