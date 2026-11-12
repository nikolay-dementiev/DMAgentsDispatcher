"""The Git side of a run: a detached worktree in a private sandbox, and its cleanup.

The user's checkout is only read. The worktree is created detached at the current HEAD, so no
branch exists until the SDK seals a result. Hooks, fsmonitor and optional locks are neutralised
by `GitCli` on every call.
"""

import shutil
from pathlib import Path

from agents_dispatcher.adapters.system.git_cli import GitCli
from agents_dispatcher.application.errors import WorkspaceError, WorkspaceNotGitError
from agents_dispatcher.application.ports import PreparedWorkspace
from agents_dispatcher.domain.run import RunId

SANDBOX_MODE = 0o700


class GitWorkspace:
    """A `Workspace` backed by `git worktree` under a run area."""

    def __init__(self, run_area: str, *, git: GitCli | None = None) -> None:
        """Keep the run area (created on first use) and the Git wrapper."""
        self._run_area = run_area
        self._git = git or GitCli()
        self._repos: dict[str, str] = {}

    def prepare(self, repo: str, run_id: RunId) -> PreparedWorkspace:
        """Create the sandbox and worktree, or refuse having created nothing."""
        top = self._repository_root(repo)
        base = self._base_commit(top)
        notes: list[str] = []
        if Path(repo).resolve() != Path(top).resolve():
            notes.append("the repository root was used instead of the given subdirectory")
        if self._git.run(top, "status", "--porcelain=v1", "-z").stdout:
            notes.append("uncommitted changes in the repository are not included")
        sandbox = Path(self._run_area) / run_id.sandbox_name
        worktree = sandbox / "worktree"
        Path(self._run_area).mkdir(mode=SANDBOX_MODE, parents=True, exist_ok=True)
        sandbox.mkdir(mode=SANDBOX_MODE)
        try:
            self._git.run(top, "worktree", "add", "--detach", str(worktree), base)
        except WorkspaceError:
            shutil.rmtree(sandbox)
            raise
        self._repos[str(sandbox)] = top
        return PreparedWorkspace(
            base_commit=base,
            sandbox_dir=str(sandbox),
            worktree_dir=str(worktree),
            branch_name=run_id.branch_name,
            notes=tuple(notes),
        )

    def retire(self, prepared: PreparedWorkspace, *, keep_sandbox: bool) -> None:
        """Remove the sandbox and the worktree registration, unless the sandbox is evidence."""
        if keep_sandbox:
            return
        repository = self._repos.pop(prepared.sandbox_dir, None) or self._common_dir(prepared)
        shutil.rmtree(prepared.sandbox_dir)
        self._git.run(repository, "worktree", "prune")

    def _repository_root(self, repo: str) -> str:
        """Return the top-level directory of the repository containing `repo`."""
        if not Path(repo).is_dir():
            message = f"{repo} is not a directory"
            raise WorkspaceNotGitError(message)
        result = self._git.run(repo, "rev-parse", "--show-toplevel", check=False)
        if result.returncode != 0:
            message = f"{repo} is not a Git repository: {result.stderr_line}"
            raise WorkspaceNotGitError(message)
        return result.stdout.decode("utf-8").strip()

    def _base_commit(self, top: str) -> str:
        """Return the commit the worktree starts from."""
        result = self._git.run(top, "rev-parse", "--verify", "HEAD^{commit}", check=False)
        if result.returncode != 0:
            message = f"{top} has no commit to start from: {result.stderr_line}"
            raise WorkspaceError(message)
        return result.stdout.decode("utf-8").strip()

    def _common_dir(self, prepared: PreparedWorkspace) -> str:
        """Find the repository of a worktree this adapter instance did not create."""
        result = self._git.run(
            prepared.worktree_dir, "rev-parse", "--path-format=absolute", "--git-common-dir"
        )
        return result.stdout.decode("utf-8").strip()
