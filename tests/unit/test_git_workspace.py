"""Tests of the Git workspace adapter against real temporary repositories."""

import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from agents_dispatcher.adapters.system.git_workspace import GitWorkspace

from agents_dispatcher.application.errors import WorkspaceError, WorkspaceNotGitError
from tests.support.fake_executable import FakeExecutable, SourceRef
from tests.support.git_repo import checkout_bytes, git, make_repo
from tests.support.run_spies import RUN_ID


def _git_version() -> str:
    """Return the installed Git version, which anchors the hook fakes to a real version."""
    text = subprocess.run(
        ["git", "--version"],  # noqa: S607 - git from PATH is the point
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    match = re.search(r"\d+(\.\d+)+", text)
    return match.group(0) if match else "2.0.0"


HOOK_SOURCE = SourceRef(
    kind="spec_excerpt",
    cli_name="git",
    cli_version=_git_version(),
    excerpt="githooks(5): hooks such as post-checkout run when core.hooksPath is not disabled",
)


class WorkspaceCase(unittest.TestCase):
    """A temporary repository, a run area and an adapter."""

    def setUp(self) -> None:
        """Create the repository and the adapter."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.repo = make_repo(self.root / "repo")
        self.run_area = self.root / "runs"
        self.workspace = GitWorkspace(str(self.run_area))


class RefusalTests(WorkspaceCase):
    """What is refused, and that refusing creates nothing."""

    def test_a_directory_that_is_not_a_repository_is_refused_and_creates_nothing(self) -> None:
        """WORKSPACE_NOT_GIT leaves no run area behind."""
        plain = self.root / "plain"
        plain.mkdir()
        with self.assertRaises(WorkspaceNotGitError):
            self.workspace.prepare(str(plain), RUN_ID)
        self.assertFalse(self.run_area.exists(), msg="the run area was not created")
        self.assertEqual(list(plain.iterdir()), [], msg="the directory was not touched")

    def test_a_repository_without_commits_is_refused(self) -> None:
        """There is no base commit to start from."""
        empty = self.root / "empty"
        empty.mkdir()
        git(empty, "init", "-q")
        with self.assertRaises(WorkspaceError):
            self.workspace.prepare(str(empty), RUN_ID)
        self.assertFalse(self.run_area.exists(), msg="the run area was not created")


class PrepareTests(WorkspaceCase):
    """The sandbox and the worktree."""

    def test_prepare_creates_a_private_sandbox_with_a_detached_worktree(self) -> None:
        """The worktree holds the base commit and sits in the run sandbox."""
        prepared = self.workspace.prepare(self.repo, RUN_ID)
        head = git(self.repo, "rev-parse", "HEAD").strip()
        self.assertEqual(prepared.base_commit, head, msg="base")
        self.assertEqual(
            prepared.sandbox_dir, str(self.run_area / RUN_ID.sandbox_name), msg="sandbox"
        )
        self.assertEqual(prepared.worktree_dir, f"{prepared.sandbox_dir}/worktree", msg="worktree")
        self.assertEqual(prepared.branch_name, RUN_ID.branch_name, msg="branch name")
        self.assertEqual(
            (Path(prepared.worktree_dir) / "README.md").read_text(encoding="utf-8"),
            "hello\n",
            msg="files of the base commit are checked out",
        )
        self.assertEqual(
            git(prepared.worktree_dir, "rev-parse", "--abbrev-ref", "HEAD").strip(),
            "HEAD",
            msg="the worktree is detached, so no branch exists yet",
        )
        self.assertEqual(Path(prepared.sandbox_dir).stat().st_mode & 0o777, 0o700, msg="private")

    def test_prepare_leaves_the_user_checkout_byte_identical(self) -> None:
        """Compared byte for byte, not through git status."""
        before = checkout_bytes(self.repo)
        self.workspace.prepare(self.repo, RUN_ID)
        self.assertEqual(checkout_bytes(self.repo), before, msg="files, index and refs unchanged")

    def test_uncommitted_changes_are_not_copied_and_are_reported(self) -> None:
        """The worktree starts from the commit; the note tells the caller."""
        (Path(self.repo) / "README.md").write_text("edited\n", encoding="utf-8")
        prepared = self.workspace.prepare(self.repo, RUN_ID)
        self.assertEqual(
            (Path(prepared.worktree_dir) / "README.md").read_text(encoding="utf-8"),
            "hello\n",
            msg="committed content, not the edit",
        )
        self.assertTrue(
            any("uncommitted" in note for note in prepared.notes), msg="a note names the omission"
        )

    def test_a_subdirectory_selects_the_repository_root_and_says_so(self) -> None:
        """M1 always works on the whole repository."""
        nested = Path(self.repo) / "pkg"
        nested.mkdir()
        (nested / "a.txt").write_text("a\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "add pkg")
        prepared = self.workspace.prepare(str(nested), RUN_ID)
        self.assertTrue((Path(prepared.worktree_dir) / "README.md").exists(), msg="root checkout")
        self.assertTrue(any("subdirectory" in note for note in prepared.notes), msg="note")

    def test_hooks_and_fsmonitor_of_the_user_repository_do_not_run(self) -> None:
        """A planted post-checkout hook and an fsmonitor command must stay silent."""
        marker = self.root / "marker"
        script = FakeExecutable("plant", f'echo ran >> "{marker}"\n', HOOK_SOURCE)
        hooks = Path(self.repo) / ".git" / "hooks"
        script.write_to(hooks).rename(hooks / "post-checkout")
        monitor = script.write_to(self.root)
        git(self.repo, "config", "core.fsmonitor", str(monitor))
        self.workspace.prepare(self.repo, RUN_ID)
        self.assertFalse(marker.exists(), msg="neither the hook nor fsmonitor ran")


class RetireTests(WorkspaceCase):
    """Cleanup."""

    def test_retire_removes_the_worktree_registration_and_the_sandbox(self) -> None:
        """Nothing of the run remains in the repository or in the run area."""
        prepared = self.workspace.prepare(self.repo, RUN_ID)
        self.workspace.retire(prepared, keep_sandbox=False)
        self.assertFalse(Path(prepared.sandbox_dir).exists(), msg="sandbox removed")
        listed = git(self.repo, "worktree", "list", "--porcelain")
        self.assertEqual(listed.count("worktree "), 1, msg="only the main worktree is registered")

    def test_retire_can_keep_the_sandbox_for_inspection(self) -> None:
        """Surviving processes make the sandbox evidence."""
        prepared = self.workspace.prepare(self.repo, RUN_ID)
        self.workspace.retire(prepared, keep_sandbox=True)
        self.assertTrue(Path(prepared.worktree_dir).exists(), msg="worktree kept")


if __name__ == "__main__":
    unittest.main()
