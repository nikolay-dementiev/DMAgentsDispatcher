"""Tests of the sandbox a held run left behind: sealed and removed by another process, later."""

import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from agents_dispatcher.adapters.system.git_workspace import GitWorkspace
from agents_dispatcher.adapters.system.kept_sandbox import KeptSandbox
from agents_dispatcher.application.errors import WorkspaceError
from agents_dispatcher.application.ports import HeldRun, SealedResult
from agents_dispatcher.domain.routing import TaskMode
from agents_dispatcher.domain.run import RunState
from tests.support.git_repo import git, make_repo
from tests.support.run_spies import RUN_ID


class KeptCase(unittest.TestCase):
    """A run prepared by one adapter instance, as a supervisor does, and a second one to resolve."""

    def setUp(self) -> None:
        """Prepare a worktree, let the worker write into it, and build the resolving side."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.repo = make_repo(self.root / "repo")
        self.run_area = self.root / "runs"
        prepared = GitWorkspace(str(self.run_area)).prepare(self.repo, RUN_ID)
        self.prepared = prepared
        (Path(prepared.worktree_dir) / "result.txt").write_text("done\n", encoding="utf-8")
        self.held = HeldRun(
            run_id=RUN_ID,
            state=RunState.QUARANTINED,
            repo=self.repo,
            base_commit=prepared.base_commit,
            mode=TaskMode.WRITE,
            released_at=None,
            absence_verified_at=None,
        )
        self.kept = KeptSandbox(str(self.run_area))

    def branch_tree(self) -> str:
        """Return the file list of the result branch."""
        return git(self.repo, "ls-tree", "-r", "--name-only", RUN_ID.branch_name)


class SealTests(KeptCase):
    """Publishing the result of a run whose supervisor is gone."""

    def test_a_second_process_seals_what_the_worker_left(self) -> None:
        """The result branch holds the new file, made by a fresh adapter instance."""
        sealed = self.kept.seal(self.held)
        self.assertEqual(sealed, SealedResult(RUN_ID.branch_name, 1), msg="the sealed result")
        self.assertIn("result.txt", self.branch_tree(), msg="the worker's file is on the branch")

    def test_a_worker_that_rewrote_the_git_pointer_cannot_redirect_the_seal(self) -> None:
        """The git directory comes from the repository's own record, not from the worktree file."""
        decoy = self.root / "decoy.git"
        git(self.root, "init", "-q", "--bare", str(decoy))
        pointer = Path(self.prepared.worktree_dir) / ".git"
        pointer.write_text(f"gitdir: {decoy}\n", encoding="utf-8")
        self.kept.seal(self.held)
        self.assertIn("result.txt", self.branch_tree(), msg="sealed into the real repository")
        self.assertEqual(
            list((decoy / "objects").glob("??")), [], msg="and nothing was written to the decoy"
        )

    def test_a_seal_that_already_happened_is_not_an_error(self) -> None:
        """A resolve that died after the seal can be run again."""
        first = self.kept.seal(self.held)
        again = self.kept.seal(self.held)
        self.assertEqual(again, first, msg="the same result")

    def test_a_run_without_a_base_commit_cannot_be_sealed(self) -> None:
        """Nothing to compare the worktree with."""
        no_base = HeldRun(RUN_ID, RunState.QUARANTINED, self.repo, None, TaskMode.WRITE, None, None)
        with self.assertRaises(WorkspaceError, msg="no base"):
            self.kept.seal(no_base)

    def test_a_repository_that_is_gone_cannot_be_sealed(self) -> None:
        """The failure is named, so the owner can release the run instead."""
        gone = HeldRun(
            RUN_ID,
            RunState.QUARANTINED,
            str(self.root / "missing"),
            "a" * 40,
            TaskMode.WRITE,
            None,
            None,
        )
        with self.assertRaises(WorkspaceError, msg="missing repository"):
            self.kept.seal(gone)

    def test_a_worktree_swapped_for_a_link_is_refused(self) -> None:
        """A worker that replaced its folder by a link must not make the seal read elsewhere."""
        elsewhere = self.root / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "secret.txt").write_text("not the worker's\n", encoding="utf-8")
        worktree = Path(self.prepared.worktree_dir)
        shutil.rmtree(worktree)
        worktree.symlink_to(elsewhere)
        with self.assertRaises(WorkspaceError, msg="the link is refused"):
            self.kept.seal(self.held)
        self.assertNotIn(
            RUN_ID.branch_name, git(self.repo, "branch", "--list"), msg="and nothing was published"
        )

    def test_a_base_commit_that_is_not_a_full_hash_is_refused(self) -> None:
        """The stored value reaches git as an argument; only a full hash may."""
        hostile = HeldRun(
            RUN_ID, RunState.QUARANTINED, self.repo, "HEAD", TaskMode.WRITE, None, None
        )
        with self.assertRaises(WorkspaceError, msg="not a hash"):
            self.kept.seal(hostile)

    def test_an_index_left_by_an_earlier_seal_does_not_leak_into_the_tree(self) -> None:
        """The private index starts empty, so a stale entry cannot reach the result branch."""
        planted = self.root / "planted.txt"
        planted.write_text("planted\n", encoding="utf-8")
        blob = git(self.repo, "hash-object", "-w", str(planted)).strip()
        stale = Path(self.prepared.sandbox_dir) / "seal.index"
        subprocess.run(  # noqa: S603 - fixed argv, no shell
            [  # noqa: S607 - git from PATH is the point
                "git",
                "-C",
                self.repo,
                "update-index",
                "--add",
                "--cacheinfo",
                f"100644,{blob},planted.txt",
            ],
            env={
                "PATH": os.environ.get("PATH", ""),
                "HOME": str(self.root),
                "GIT_INDEX_FILE": str(stale),
            },
            check=True,
        )
        self.kept.seal(self.held)
        self.assertNotIn("planted.txt", self.branch_tree(), msg="the stale entry is not sealed")


class RemoveTests(KeptCase):
    """Taking the sandbox away once nothing can use it."""

    def test_removing_deletes_the_sandbox_and_the_worktree_registration(self) -> None:
        """Nothing of the run is left under the run area or in the repository's list."""
        self.kept.remove(self.held)
        self.assertFalse(Path(self.prepared.sandbox_dir).exists(), msg="the sandbox is gone")
        self.assertNotIn(
            RUN_ID.sandbox_name,
            git(self.repo, "worktree", "list", "--porcelain"),
            msg="and the repository no longer lists it",
        )

    def test_a_result_branch_survives_the_removal(self) -> None:
        """Result branches are never deleted by the SDK."""
        self.kept.seal(self.held)
        self.kept.remove(self.held)
        self.assertIn("result.txt", self.branch_tree(), msg="the branch is still there")


if __name__ == "__main__":
    unittest.main()
