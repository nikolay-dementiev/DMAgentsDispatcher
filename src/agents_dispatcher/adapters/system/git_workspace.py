"""The Git side of a run: a detached worktree in a private sandbox, and its cleanup.

The user's checkout is only read. The worktree is created detached at the current HEAD, so no
branch exists until the SDK seals a result. Hooks, fsmonitor and optional locks are neutralised
by `GitCli` on every call.
"""

import os
import re
import shutil
import stat
from pathlib import Path

from agents_dispatcher.adapters.system.git_cli import GitCli
from agents_dispatcher.adapters.system.private_folder import ensure_private_folder
from agents_dispatcher.application.errors import WorkspaceError, WorkspaceNotGitError
from agents_dispatcher.application.ports import PreparedWorkspace, SealedResult
from agents_dispatcher.domain.run import RunId

_FULL_HASH = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

SANDBOX_MODE = 0o700
OWNER_EXECUTE_BIT = 0o100
SDK_IDENTITY = {
    "GIT_AUTHOR_NAME": "agents-dispatcher",
    "GIT_AUTHOR_EMAIL": "agentsd@localhost.invalid",
    "GIT_COMMITTER_NAME": "agents-dispatcher",
    "GIT_COMMITTER_EMAIL": "agentsd@localhost.invalid",
}
MODE_FILE = "100644"
MODE_EXECUTABLE = "100755"
MODE_SYMLINK = "120000"
MODE_GITLINK = "160000"


class GitWorkspace:
    """A `Workspace` backed by `git worktree` under a run area."""

    def __init__(self, run_area: str, *, git: GitCli | None = None) -> None:
        """Keep the run area (created on first use) and the Git wrapper."""
        self._run_area = run_area
        self._git = git or GitCli()
        self._repos: dict[str, str] = {}
        self._gitdirs: dict[str, str] = {}

    def require_git(self, repo: str) -> None:
        """Refuse what `prepare` would refuse, reading the repository and creating nothing."""
        self._base_commit(self._repository_root(repo))

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
        ensure_private_folder(Path(self._run_area), SANDBOX_MODE, WorkspaceError)
        sandbox.mkdir(mode=SANDBOX_MODE)
        try:
            self._git.run(top, "worktree", "add", "--detach", str(worktree), base)
        except WorkspaceError:
            shutil.rmtree(sandbox)
            raise
        self._repos[str(sandbox)] = top
        # Read now, before the worker ran: the worker can rewrite `worktree/.git` later, so every
        # later call names this directory explicitly instead of trusting the file.
        self._gitdirs[str(sandbox)] = (
            self._git.run(str(worktree), "rev-parse", "--path-format=absolute", "--git-dir")
            .stdout.decode("utf-8")
            .strip()
        )
        return PreparedWorkspace(
            base_commit=base,
            run_id=run_id,
            sandbox_dir=str(sandbox),
            worktree_dir=str(worktree),
            branch_name=run_id.branch_name,
            notes=tuple(notes),
        )

    def adopt(self, repo: str, run_id: RunId, base_commit: str) -> PreparedWorkspace:
        """Take over a workspace that another instance prepared, trusting only the repository.

        The worker can rewrite `worktree/.git`, so the git directory is found among the
        repository's own worktree records, never read from the worktree.
        """
        try:
            top = self._repository_root(repo)
        except WorkspaceNotGitError as error:
            raise WorkspaceError(str(error)) from error
        if _FULL_HASH.fullmatch(base_commit) is None:
            message = f"the recorded base commit {base_commit!r} is not a full commit hash"
            raise WorkspaceError(message)
        sandbox = Path(self._run_area) / run_id.sandbox_name
        worktree = sandbox / "worktree"
        _require_own_folder(sandbox)
        _require_own_folder(worktree)
        common = (
            self._git.run(top, "rev-parse", "--path-format=absolute", "--git-common-dir")
            .stdout.decode("utf-8")
            .strip()
        )
        gitdir = _registered_gitdir(Path(common), worktree)
        if gitdir is None:
            message = f"the repository does not list a worktree at {worktree}"
            raise WorkspaceError(message)
        self._repos[str(sandbox)] = top
        self._gitdirs[str(sandbox)] = str(gitdir)
        return PreparedWorkspace(
            base_commit=base_commit,
            run_id=run_id,
            sandbox_dir=str(sandbox),
            worktree_dir=str(worktree),
            branch_name=run_id.branch_name,
        )

    def sealed_result(self, prepared: PreparedWorkspace) -> SealedResult | None:
        """Return the result a seal already published for this run, or None."""
        top = self._repository_for(prepared)
        found = self._git.run(
            top, "rev-parse", "--verify", "-q", f"refs/heads/{prepared.branch_name}", check=False
        )
        if found.returncode != 0:
            return None
        return SealedResult(branch=prepared.branch_name, commits_on_branch=1)

    def seal(self, prepared: PreparedWorkspace) -> SealedResult:
        """Publish what the worker left as one commit on the run branch, or nothing.

        Files are read from the worktree and hashed without filters into a private index, so the
        user's index, files and other refs are never written and no repository-supplied driver
        runs. The branch is created with expected absence, so a second seal fails.
        """
        top = self._repository_for(prepared)
        index_file = Path(prepared.sandbox_dir) / "seal.index"
        index_file.unlink(missing_ok=True)
        env = {"GIT_INDEX_FILE": str(index_file), **SDK_IDENTITY}
        try:
            entries = self._worktree_entries(prepared, top)
            payload = b"".join(
                b"%s %s\t%s\0" % (mode.encode(), sha.encode(), os.fsencode(path))
                for path, (mode, sha) in sorted(entries.items())
            )
            self._git.run(top, "update-index", "-z", "--index-info", stdin=payload, env=env)
            tree = self._git.run(top, "write-tree", env=env).stdout.decode().strip()
            base_tree = (
                self._git.run(top, "rev-parse", f"{prepared.base_commit}^{{tree}}")
                .stdout.decode()
                .strip()
            )
            if tree == base_tree:
                return SealedResult(branch=None, commits_on_branch=0)
            commit = (
                self._git.run(
                    top,
                    "-c",
                    "commit.gpgsign=false",
                    "commit-tree",
                    tree,
                    "-p",
                    prepared.base_commit,
                    "-m",
                    f"Result of {prepared.branch_name}",
                    env=SDK_IDENTITY,
                )
                .stdout.decode()
                .strip()
            )
            self._git.run(
                top, "update-ref", f"refs/heads/{prepared.branch_name}", commit, "0" * len(commit)
            )
        finally:
            index_file.unlink(missing_ok=True)
        return SealedResult(branch=prepared.branch_name, commits_on_branch=1)

    def retire(self, prepared: PreparedWorkspace, *, keep_sandbox: bool) -> None:
        """Remove the sandbox and the worktree registration, unless the sandbox is evidence."""
        if keep_sandbox:
            return
        repository = self._repos.get(prepared.sandbox_dir)
        if repository is None:
            raise _unprepared(prepared)
        del self._repos[prepared.sandbox_dir]
        self._gitdirs.pop(prepared.sandbox_dir, None)
        if Path(prepared.sandbox_dir).exists():
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

    def _repository_for(self, prepared: PreparedWorkspace) -> str:
        """Return the repository of a workspace this instance prepared; refuse any other."""
        repository = self._repos.get(prepared.sandbox_dir)
        if repository is None:
            raise _unprepared(prepared)
        return repository

    def _worktree_entries(
        self, prepared: PreparedWorkspace, top: str
    ) -> dict[str, tuple[str, str]]:
        """Map each path of the sealed tree to (mode, blob id), read from the worktree."""
        worktree = Path(prepared.worktree_dir)
        base = self._git.run(top, "ls-tree", "-r", "-z", prepared.base_commit).stdout
        tracked: dict[str, tuple[str, str]] = {}
        for record in filter(None, base.split(b"\0")):
            meta, raw_path = record.split(b"\t", 1)
            mode, _kind, sha = meta.decode().split(" ")
            tracked[os.fsdecode(raw_path)] = (mode, sha)
        others = self._git.run(
            prepared.worktree_dir,
            *self._explicit_dirs(prepared),
            "ls-files",
            "--others",
            "--exclude-standard",
            "-z",
        ).stdout
        candidates = [*tracked, *(os.fsdecode(p) for p in filter(None, others.split(b"\0")))]
        entries: dict[str, tuple[str, str]] = {}
        files: dict[str, str] = {}
        links: list[str] = []
        for path in candidates:
            if tracked.get(path, ("", ""))[0] == MODE_GITLINK:
                entries[path] = tracked[path]
                continue
            if _below_symlink(worktree, path):
                continue  # the path exists only through a link the worker created
            try:
                status = (worktree / path).lstat()
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(status.st_mode):
                links.append(path)
            elif stat.S_ISREG(status.st_mode):
                executable = status.st_mode & OWNER_EXECUTE_BIT
                files[path] = MODE_EXECUTABLE if executable else MODE_FILE
        for path, sha in zip(files, self._hash_files(prepared, list(files)), strict=True):
            entries[path] = (files[path], sha)
        for path in links:
            target = os.fsencode((worktree / path).readlink())
            sha = (
                self._git.run(
                    prepared.worktree_dir,
                    *self._explicit_dirs(prepared),
                    "hash-object",
                    "-w",
                    "--no-filters",
                    "--stdin",
                    stdin=target,
                )
                .stdout.decode()
                .strip()
            )
            entries[path] = (MODE_SYMLINK, sha)
        return entries

    def _hash_files(self, prepared: PreparedWorkspace, paths: list[str]) -> list[str]:
        """Hash regular files without filters in one call; names with line breaks are refused."""
        if not paths:
            return []
        for path in paths:
            if "\n" in path or "\r" in path:
                message = f"path {path!r} contains a line break and cannot be sealed"
                raise WorkspaceError(message)
        listing = "".join(f"{path}\n" for path in paths).encode("utf-8", errors="surrogateescape")
        result = self._git.run(
            prepared.worktree_dir,
            *self._explicit_dirs(prepared),
            "hash-object",
            "-w",
            "--no-filters",
            "--stdin-paths",
            stdin=listing,
        )
        shas = result.stdout.decode().split()
        if len(shas) != len(paths):
            message = f"git hash-object returned {len(shas)} ids for {len(paths)} files"
            raise WorkspaceError(message)
        return shas

    def _explicit_dirs(self, prepared: PreparedWorkspace) -> tuple[str, str]:
        """Return `--git-dir` and `--work-tree` taken from the supervisor's own records."""
        # Written together with `_repos` in `prepare`; callers have passed `_repository_for`.
        gitdir = self._gitdirs[prepared.sandbox_dir]
        return (f"--git-dir={gitdir}", f"--work-tree={prepared.worktree_dir}")


def _require_own_folder(folder: Path) -> None:
    """Refuse a folder that is a link, is not a folder, or belongs to another user.

    The worker runs beside these folders; a link put in place of one would make a later seal read
    or remove files outside the run.
    """
    try:
        info = folder.lstat()
    except OSError as error:
        message = f"cannot inspect {folder}: {error}"
        raise WorkspaceError(message) from error
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid():
        message = f"{folder} is not a folder of this user: it is left alone"
        raise WorkspaceError(message)


def _registered_gitdir(common: Path, worktree: Path) -> Path | None:
    """Return the repository's admin folder whose record names `worktree`, if there is one."""
    wanted = worktree.resolve() / ".git"
    for admin in sorted((common / "worktrees").glob("*")):
        try:
            recorded = Path((admin / "gitdir").read_text(encoding="utf-8").strip())
        except OSError:
            continue  # a half-made or half-removed record names no worktree
        if recorded.resolve() == wanted:
            return admin
    return None


def _unprepared(prepared: PreparedWorkspace) -> WorkspaceError:
    """Build the refusal for a workspace that this adapter instance did not prepare."""
    message = f"workspace {prepared.sandbox_dir} was not prepared by this workspace instance"
    return WorkspaceError(message)


def _below_symlink(worktree: Path, path: str) -> bool:
    """Return whether a parent component of `path` is a symlink (the final component may be)."""
    current = worktree
    for part in Path(path).parts[:-1]:
        current = current / part
        if current.is_symlink():
            return True
    return False
