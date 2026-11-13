"""Tests of the Git workspace adapter against real temporary repositories."""

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from agents_dispatcher.adapters.system.git_workspace import GitWorkspace
from agents_dispatcher.application.errors import WorkspaceError, WorkspaceNotGitError
from agents_dispatcher.application.ports import PreparedWorkspace, SealedResult
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


class UnpreparedWorkspaceTests(WorkspaceCase):
    """An adapter instance acts only on workspaces it prepared itself."""

    def test_another_instance_refuses_to_seal_and_to_retire_with_a_clear_error(self) -> None:
        """No fallback that trusts the worktree's own `.git` file: refuse and leave it alone."""
        prepared = self.workspace.prepare(self.repo, RUN_ID)
        stranger = GitWorkspace(str(self.run_area))
        with self.assertRaises(WorkspaceError, msg="seal") as sealing:
            stranger.seal(prepared)
        with self.assertRaises(WorkspaceError, msg="retire") as retiring:
            stranger.retire(prepared, keep_sandbox=False)
        self.assertIn("not prepared by this workspace", str(sealing.exception), msg="seal text")
        self.assertIn("not prepared by this workspace", str(retiring.exception), msg="retire text")
        self.assertTrue(Path(prepared.sandbox_dir).exists(), msg="the sandbox is left alone")


class SealTests(WorkspaceCase):
    """Publishing what the worker left, without touching the user's index or files."""

    def setUp(self) -> None:
        """Use a repository with an ignore rule and executable files."""
        super().setUp()
        directory = self.root / "rich"
        self.repo = make_repo(
            directory,
            {
                "README.md": "hello\n",
                ".gitignore": "*.log\n",
                "old.txt": "old\n",
                "src/keep.sh": "echo keep\n",
                "src/drop.sh": "echo drop\n",
            },
        )
        git(self.repo, "update-index", "--chmod=+x", "src/keep.sh", "src/drop.sh")
        git(self.repo, "commit", "-q", "-m", "make scripts executable")
        git(self.repo, "checkout", "--", "src/keep.sh", "src/drop.sh")
        self.prepared: PreparedWorkspace = self.workspace.prepare(self.repo, RUN_ID)
        self.tree = Path(self.prepared.worktree_dir)

    def sealed_tree(self) -> dict[str, str]:
        """Map path to 'mode content' for the sealed branch."""
        listing = git(self.repo, "ls-tree", "-r", "-z", RUN_ID.branch_name).split("\0")
        result = {}
        for line in filter(None, listing):
            meta, path = line.split("\t", 1)
            mode, _, sha = meta.split()
            result[path] = f"{mode} {git(self.repo, 'cat-file', '-p', sha)}"
        return result

    def test_a_worker_that_changed_nothing_publishes_no_branch(self) -> None:
        """Q4: no branch and zero commits."""
        sealed = self.workspace.seal(self.prepared)
        self.assertEqual(sealed, SealedResult(branch=None, commits_on_branch=0), msg="result")
        self.assertEqual(git(self.repo, "branch", "--list", "agentsd-run-*").strip(), "", msg="ref")

    def test_changes_are_sealed_as_one_commit_on_the_run_branch(self) -> None:
        """Modified, new and deleted files, the executable bit and a symlink all arrive."""
        (self.tree / "README.md").write_text("changed\n", encoding="utf-8")
        (self.tree / "new file \u00e9.txt").write_text("new\n", encoding="utf-8")
        (self.tree / "old.txt").unlink()
        (self.tree / "src" / "drop.sh").chmod(0o644)
        (self.tree / "link").symlink_to("README.md")
        sealed = self.workspace.seal(self.prepared)
        self.assertEqual(sealed, SealedResult(RUN_ID.branch_name, 1), msg="result")
        tree = self.sealed_tree()
        self.assertEqual(tree["README.md"], "100644 changed\n", msg="modified file")
        self.assertEqual(tree["new file \u00e9.txt"], "100644 new\n", msg="new file, odd name")
        self.assertNotIn("old.txt", tree, msg="deleted file is gone")
        self.assertTrue(tree["src/keep.sh"].startswith("100755"), msg="kept executable bit")
        self.assertTrue(tree["src/drop.sh"].startswith("100644"), msg="removed executable bit")
        self.assertEqual(tree["link"], "120000 README.md", msg="symlink stored as a link")
        count = git(
            self.repo, "rev-list", "--count", f"{self.prepared.base_commit}..{RUN_ID.branch_name}"
        )
        self.assertEqual(count.strip(), "1", msg="exactly one commit on top of the base")
        parent = git(self.repo, "rev-parse", f"{RUN_ID.branch_name}^").strip()
        self.assertEqual(parent, self.prepared.base_commit, msg="parent is the base commit")

    def test_ignored_files_are_left_out(self) -> None:
        """Build output and logs do not travel."""
        (self.tree / "debug.log").write_text("noise\n", encoding="utf-8")
        (self.tree / "keep.txt").write_text("x\n", encoding="utf-8")
        self.workspace.seal(self.prepared)
        self.assertNotIn("debug.log", self.sealed_tree(), msg="ignored file excluded")
        self.assertIn("keep.txt", self.sealed_tree(), msg="other new file included")

    def test_the_commit_carries_the_sdk_identity_and_the_run_id(self) -> None:
        """The user's own name and email are not used."""
        (self.tree / "a.txt").write_text("a\n", encoding="utf-8")
        self.workspace.seal(self.prepared)
        author = git(self.repo, "log", "-1", "--format=%an <%ae>", RUN_ID.branch_name).strip()
        subject = git(self.repo, "log", "-1", "--format=%s", RUN_ID.branch_name).strip()
        self.assertEqual(author, "agents-dispatcher <agentsd@localhost.invalid>", msg="author")
        self.assertIn(RUN_ID.value, subject, msg="subject names the run")

    def test_the_user_checkout_is_byte_identical_except_for_the_new_branch(self) -> None:
        """Index, files, config and every other ref stay as they were."""
        before = checkout_bytes(self.repo, ignore=[RUN_ID.branch_name])
        (self.tree / "README.md").write_text("changed\n", encoding="utf-8")
        (self.tree / "extra.txt").write_text("extra\n", encoding="utf-8")
        self.workspace.seal(self.prepared)
        self.workspace.retire(self.prepared, keep_sandbox=False)
        after = checkout_bytes(self.repo, ignore=[RUN_ID.branch_name])
        self.assertEqual(after, before, msg="checkout unchanged")
        self.assertEqual(
            git(self.repo, "status", "--porcelain").strip(), "", msg="and git agrees it is clean"
        )

    def test_hooks_and_clean_filters_do_not_run_while_sealing(self) -> None:
        """A reference-transaction hook and a clean filter of the user's repository stay silent."""
        marker = self.root / "sealed-marker"
        script = FakeExecutable("plant", f'cat\necho ran >> "{marker}"\n', HOOK_SOURCE)
        hooks = Path(self.repo) / ".git" / "hooks"
        script.write_to(hooks).rename(hooks / "reference-transaction")
        cleaner = script.write_to(self.root)
        git(self.repo, "config", "filter.evil.clean", str(cleaner))
        (self.tree / ".gitattributes").write_text("* filter=evil\n", encoding="utf-8")
        (self.tree / "README.md").write_text("changed\n", encoding="utf-8")
        self.workspace.seal(self.prepared)
        self.assertFalse(marker.exists(), msg="no hook and no filter driver ran")
        self.assertEqual(self.sealed_tree()["README.md"], "100644 changed\n", msg="raw bytes")

    def test_a_directory_swapped_for_a_symlink_does_not_leak_what_it_points_at(self) -> None:
        """M1: a tracked path below a symlinked directory must not be read through the link."""
        secret = self.root / "secret"
        secret.mkdir()
        (secret / "keep.sh").write_text("SECRET\n", encoding="utf-8")
        shutil.rmtree(self.tree / "src")
        (self.tree / "src").symlink_to(secret)
        self.workspace.seal(self.prepared)
        leaked = [path for path, value in self.sealed_tree().items() if "SECRET" in value]
        self.assertEqual(leaked, [], msg="no sealed file holds the content behind the link")
        self.assertNotIn("src/keep.sh", self.sealed_tree(), msg="the path under the link is gone")
        self.assertTrue(self.sealed_tree()["src"].startswith("120000"), msg="src is a symlink")

    def test_a_redirected_git_file_in_the_worktree_is_not_trusted(self) -> None:
        """M2: the worker may rewrite worktree/.git; sealing must use the supervisor's own paths."""
        other = Path(make_repo(self.root / "other"))
        before = git(other, "count-objects", "-v")
        (self.tree / ".git").unlink()
        (self.tree / ".git").write_text(f"gitdir: {other}/.git\n", encoding="utf-8")
        (self.tree / "README.md").write_text("changed\n", encoding="utf-8")
        sealed = self.workspace.seal(self.prepared)
        self.assertEqual(sealed.branch, RUN_ID.branch_name, msg="the result is still sealed")
        self.assertEqual(self.sealed_tree()["README.md"], "100644 changed\n", msg="right content")
        self.assertEqual(git(other, "count-objects", "-v"), before, msg="nothing written elsewhere")

    def test_a_redirected_git_file_cannot_hide_or_invent_untracked_files(self) -> None:
        """M2: the list of new files comes from the supervisor's Git directory, not the worker's."""
        decoy = Path(make_repo(self.root / "decoy", {"new.txt": "decoy\n"}))
        (self.tree / "new.txt").write_text("worker file\n", encoding="utf-8")
        (self.tree / ".git").unlink()
        (self.tree / ".git").write_text(f"gitdir: {decoy}/.git\n", encoding="utf-8")
        self.workspace.seal(self.prepared)
        self.assertEqual(
            self.sealed_tree().get("new.txt"), "100644 worker file\n", msg="the new file is sealed"
        )

    def test_a_second_seal_cannot_overwrite_the_branch(self) -> None:
        """The branch is created with expected absence."""
        (self.tree / "a.txt").write_text("a\n", encoding="utf-8")
        self.workspace.seal(self.prepared)
        first = git(self.repo, "rev-parse", RUN_ID.branch_name).strip()
        (self.tree / "b.txt").write_text("b\n", encoding="utf-8")
        with self.assertRaises(WorkspaceError):
            self.workspace.seal(self.prepared)
        self.assertEqual(git(self.repo, "rev-parse", RUN_ID.branch_name).strip(), first, msg="kept")

    def test_a_path_with_a_newline_is_refused(self) -> None:
        """Such a name cannot be passed safely to the batch hashing call."""
        (self.tree / "bad\nname.txt").write_text("x\n", encoding="utf-8")
        with self.assertRaises(WorkspaceError):
            self.workspace.seal(self.prepared)
        self.assertEqual(git(self.repo, "branch", "--list", "agentsd-run-*").strip(), "", msg="ref")


if __name__ == "__main__":
    unittest.main()
