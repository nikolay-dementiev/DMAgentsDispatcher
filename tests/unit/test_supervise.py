"""Tests of the supervised run: the supervisor adopts a run that `start` accepted."""

import unittest

from agents_dispatcher.application.errors import RunNotClaimableError, WorkspaceError
from agents_dispatcher.application.ports import ProcessIdentity
from agents_dispatcher.domain.run import RunState, TaskSpec, TerminalReason
from tests.support.routing import ROUTING
from tests.support.run_spies import BASE, RUN_ID
from tests.unit.test_run_task import make_rig

SPEC = TaskSpec(repo="/work/example", task="fix the bug", hard_s=60.0, memory_bytes=1024)
SUPERVISOR = ProcessIdentity(pgid=777, start_time_s=5.5)


class SuperviseTests(unittest.TestCase):
    """The order of the ports and the facts that reach the store."""

    def test_the_claim_comes_first_and_the_workspace_facts_follow_the_worktree(self) -> None:
        """The claim is the acknowledgement `start` waits for, so nothing slow precedes it."""
        rig = make_rig()
        rig.task.supervise(SPEC, RUN_ID, SUPERVISOR, ROUTING)
        self.assertEqual(
            rig.journal.events[:8],
            [
                "owner.hold",
                "store.claim",
                "workspace.prepare",
                "worker.version",
                "store.workspace",
                "stops.ask",
                "watch.before",
                "egress.start",
            ],
            msg="owner, claim, worktree, facts, stop check, account look, then the proxy",
        )
        self.assertEqual(rig.journal.events[-2:], ["workspace.retire", "owner.release"], msg="end")
        self.assertNotIn("ids.new", rig.journal.events, msg="the id comes from `start`")
        self.assertNotIn("store.begin", rig.journal.events, msg="the row exists already")
        self.assertNotIn("store.state STARTING", rig.journal.events, msg="the claim did it")

    def test_the_supervisor_identity_and_workspace_facts_are_stored(self) -> None:
        """Pid and start time go with the claim; base commit and CLI version with the worktree."""
        rig = make_rig()
        rig.task.supervise(SPEC, RUN_ID, SUPERVISOR, ROUTING)
        self.assertEqual(rig.store.claimed, [SUPERVISOR], msg="claim identity")
        self.assertEqual(rig.store.workspace_facts, [(BASE, "2.1.304")], msg="facts")

    def test_the_stored_limits_are_the_ones_used(self) -> None:
        """The spec carries the limits `start` computed; the defaults do not apply again."""
        rig = make_rig()
        report = rig.task.supervise(SPEC, RUN_ID, SUPERVISOR, ROUTING)
        self.assertEqual(rig.confinement.limits_seen[0].hard_s, 60.0, msg="hard limit")
        self.assertEqual(report.run_id, RUN_ID, msg="the report names the accepted run")
        self.assertEqual(report.terminal_reason, TerminalReason.COMPLETED, msg="completed")

    def test_a_run_that_was_closed_meanwhile_is_not_taken_over(self) -> None:
        """If `start` gave up (LAUNCH_LOST) before the claim, the supervisor does nothing."""
        rig = make_rig()
        rig.store.claim_answer = False
        with self.assertRaises(RunNotClaimableError):
            rig.task.supervise(SPEC, RUN_ID, SUPERVISOR, ROUTING)
        self.assertEqual(
            rig.journal.events, ["owner.hold", "store.claim", "owner.release"], msg="nothing else"
        )

    def test_a_worktree_that_cannot_be_made_ends_the_run_as_failed_with_a_report(self) -> None:
        """The caller learns it from the report, not from a run stuck in STARTING."""
        rig = make_rig()
        rig.workspace.prepare_error = WorkspaceError("git worktree add failed: disk full")
        report = rig.task.supervise(SPEC, RUN_ID, SUPERVISOR, ROUTING)
        self.assertEqual(report.terminal_reason, TerminalReason.FAILED, msg="failed")
        self.assertEqual(report.state, RunState.TERMINAL, msg="terminal")
        self.assertIn("disk full", " ".join(report.notes), msg="the reason is in the report")
        self.assertEqual(rig.store.finished[0].terminal_reason, TerminalReason.FAILED, msg="stored")
        self.assertEqual(rig.journal.events[-1], "owner.release", msg="owner released")
        self.assertNotIn("egress.start", rig.journal.events, msg="no proxy for a dead run")


if __name__ == "__main__":
    unittest.main()
