"""End-to-end test of `agentsd quarantine resolve` against a real store, repository and sandbox."""

import json
import subprocess
import sys
import time
import unittest
import uuid
from dataclasses import replace
from pathlib import Path

from agents_dispatcher.adapters.system.git_workspace import GitWorkspace
from agents_dispatcher.adapters.system.process_table import LibprocProcessTable
from agents_dispatcher.application.ports import AcceptedRun, FinishRecord, ProcessIdentity
from agents_dispatcher.domain.run import Limits, RunId, RunState, Survivor, TerminalReason
from tests.e2e.test_background_runs import BackgroundCase
from tests.support.cli_book import make_report, report_text
from tests.support.git_repo import git
from tests.support.narrow import present
from tests.support.routing import ROUTING
from tests.support.start_times import start_time_of

WAIT_S = 10.0


class QuarantineCommandTests(BackgroundCase):
    """A held run whose leftover process is alive is settled by the command."""

    def setUp(self) -> None:
        """Hold a completed write run: sandbox with a new file, one live leftover process."""
        super().setUp()
        settings = self.settings()
        self.run_id = RunId(str(uuid.uuid4()))
        store = self.store()
        store.accept(AcceptedRun(self.run_id, str(self.repo), "t", Limits(60.0, 1 << 30), ROUTING))
        store.claim(self.run_id, ProcessIdentity(pgid=1, start_time_s=None))
        store.set_state(self.run_id, RunState.FINALIZING)
        prepared = GitWorkspace(settings.run_area_path).prepare(str(self.repo), self.run_id)
        store.record_workspace(self.run_id, base_commit=prepared.base_commit, cli_version="2.1.304")
        self.sandbox = Path(prepared.sandbox_dir)
        (Path(prepared.worktree_dir) / "result.txt").write_text("done\n", encoding="utf-8")
        report = replace(
            make_report(TerminalReason.COMPLETED),
            run_id=self.run_id,
            repo=str(self.repo),
            base_commit=prepared.base_commit,
            branch=None,
            commits_on_branch=0,
        )
        self.leftover = subprocess.Popen(
            [sys.executable, "-B", "-c", "import time; time.sleep(300)"], start_new_session=True
        )
        self.addCleanup(self.leftover.wait)  # runs after the kill: cleanups run last-in first
        self.addCleanup(self.leftover.kill)
        table = LibprocProcessTable()
        deadline = time.monotonic() + WAIT_S
        while start_time_of(table, self.leftover.pid) is None and time.monotonic() < deadline:
            time.sleep(0.05)
        start = present(start_time_of(table, self.leftover.pid))
        store.quarantine(
            self.run_id,
            FinishRecord(TerminalReason.COMPLETED, None, 0, None, 0, 3.5, report_text(report)),
            [Survivor(self.leftover.pid, start, ("group",))],
        )

    def test_status_shows_the_held_run_and_the_action_to_take(self) -> None:
        """Before the command: QUARANTINED, the survivor and the command that resolves it."""
        status = json.loads(self.cli("status", self.run_id.value).stdout)
        self.assertEqual(status["run"]["state"], "QUARANTINED", msg="held")
        self.assertEqual(
            status["quarantine"]["action"],
            f"agentsd quarantine resolve {self.run_id.value}",
            msg="the action is named",
        )

    def test_resolve_ends_the_leftover_seals_the_branch_and_removes_the_sandbox(self) -> None:
        """The whole path in one command, then the views agree the run is over."""
        outcome = self.cli("quarantine", "resolve", self.run_id.value)
        self.assertEqual(outcome.code, 0, msg=outcome.stderr)
        self.assertEqual(json.loads(outcome.stdout)["outcome"], "SETTLED", msg="settled")
        self.assertIsNotNone(self.leftover.wait(timeout=WAIT_S), msg="the leftover was ended")
        self.assertFalse(self.sandbox.exists(), msg="the sandbox is gone")
        self.assertEqual(
            git(self.repo, "show", f"{self.run_id.branch_name}:result.txt"),
            "done\n",
            msg="the worker's file is on the result branch",
        )
        status = json.loads(self.cli("status", self.run_id.value).stdout)
        self.assertEqual(status["run"]["state"], "TERMINAL", msg="the run is over")
        self.assertNotIn("quarantine", status, msg="and no longer held")
        report = json.loads(self.cli("report", self.run_id.value).stdout)
        self.assertEqual(report["terminal_reason"], "COMPLETED", msg="its own outcome stands")
        self.assertEqual(report["branch"], self.run_id.branch_name, msg="and names the branch")

    def test_release_frees_the_run_keeps_the_sandbox_and_a_later_look_removes_it(self) -> None:
        """The owner's release proves nothing; the sandbox goes once a look shows the run empty."""
        wrong = self.cli("quarantine", "resolve", self.run_id.value, "--release")
        self.assertEqual(wrong.code, 2, msg="--release alone is refused")
        released = self.cli(
            "quarantine",
            "resolve",
            self.run_id.value,
            "--release",
            "--confirm",
            self.run_id.value,
        )
        self.assertEqual(released.code, 0, msg=released.stderr)
        status = json.loads(self.cli("status", self.run_id.value).stdout)
        self.assertEqual(status["run"]["state"], "TERMINAL", msg="released runs end")
        self.assertTrue(self.sandbox.exists(), msg="the sandbox stays: nothing proved it empty")
        log = Path(self.settings().logging_directory) / "sdk.log"
        self.assertIn(
            "QUARANTINE_RELEASED",
            log.read_text(encoding="utf-8"),
            msg="the release is in the SDK log",
        )
        later = self.cli("quarantine", "resolve", self.run_id.value)
        self.assertEqual(later.code, 0, msg=later.stderr)
        self.assertEqual(json.loads(later.stdout)["outcome"], "VERIFIED", msg="absence verified")
        self.assertFalse(self.sandbox.exists(), msg="and now the sandbox is removed")


if __name__ == "__main__":
    unittest.main()
