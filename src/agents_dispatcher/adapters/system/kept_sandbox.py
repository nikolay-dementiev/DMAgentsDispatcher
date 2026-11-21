"""The sandbox a held run left behind, sealed or removed by a process that did not prepare it."""

from pathlib import Path

from agents_dispatcher.adapters.system.git_workspace import GitWorkspace
from agents_dispatcher.application.errors import WorkspaceError
from agents_dispatcher.application.ports import HeldRun, PreparedWorkspace, SealedResult


class KeptSandbox:
    """A `KeptWorkspace` that re-adopts the sandbox from the repository's own records."""

    def __init__(self, run_area: str) -> None:
        """Keep the run area the sandboxes live under."""
        self._run_area = run_area

    def seal(self, run: HeldRun) -> SealedResult:
        """Publish the result branch once; a repeated call returns the branch already there."""
        workspace, prepared = self._adopt(run)
        existing = workspace.sealed_result(prepared)
        if existing is not None:
            return existing
        return workspace.seal(prepared)

    def remove(self, run: HeldRun) -> None:
        """Remove the worktree and the sandbox; nothing to do when both are already gone."""
        try:
            workspace, prepared = self._adopt(run)
        except WorkspaceError:
            if Path(self._run_area, run.run_id.sandbox_name).exists():
                raise
            return
        workspace.retire(prepared, keep_sandbox=False)

    def _adopt(self, run: HeldRun) -> tuple[GitWorkspace, PreparedWorkspace]:
        """Build a workspace over the run area and take the run's sandbox over."""
        if run.base_commit is None:
            message = f"run {run.run_id} has no recorded base commit"
            raise WorkspaceError(message)
        workspace = GitWorkspace(self._run_area)
        return workspace, workspace.adopt(run.repo, run.run_id, run.base_commit)
