"""Tests of the RunTask use case with spies for every port."""

import unittest
from dataclasses import dataclass

from agents_dispatcher.application.ports import SealedResult
from agents_dispatcher.application.run_task import RunTask
from agents_dispatcher.domain.run import (
    Limits,
    RunReport,
    RunState,
    TaskSpec,
    TerminalReason,
)
from tests.support.clock import FakeClock
from tests.support.run_spies import (
    BASE,
    RUN_ID,
    Journal,
    SpyConfinement,
    SpyEgress,
    SpyIds,
    SpyStore,
    SpyWorker,
    SpyWorkspace,
)

DEFAULT_LIMITS = Limits(hard_s=3600.0, memory_bytes=2_147_483_648)
SPEC = TaskSpec(repo="/work/example", task="fix the bug")


@dataclass
class Rig:
    """A use case wired to spies, plus the spies."""

    task: RunTask
    journal: Journal
    workspace: SpyWorkspace
    egress: SpyEgress
    worker: SpyWorker
    confinement: SpyConfinement
    store: SpyStore


def make_rig() -> Rig:
    """Build a use case where every port is a spy sharing one journal."""
    journal = Journal()
    clock = FakeClock(start=100.0)
    workspace = SpyWorkspace(journal)
    egress = SpyEgress(journal)
    worker = SpyWorker(journal)
    confinement = SpyConfinement(journal, clock)
    store = SpyStore(journal)
    task = RunTask(
        workspace=workspace,
        egress=egress,
        worker=worker,
        confinement=confinement,
        store=store,
        clock=clock,
        ids=SpyIds(journal),
        default_limits=DEFAULT_LIMITS,
    )
    return Rig(task, journal, workspace, egress, worker, confinement, store)


class HappyPathTests(unittest.TestCase):
    """One run that completes."""

    def test_ports_are_called_in_the_designed_order(self) -> None:
        """Workspace first, proxy before the worker, cleanup last."""
        rig = make_rig()
        rig.task.run(SPEC)
        self.assertEqual(
            rig.journal.events,
            [
                "ids.new",
                "workspace.prepare",
                "worker.version",
                "store.begin",
                "store.state STARTING",
                "egress.start",
                "worker.command",
                "store.state RUNNING",
                "confinement.run",
                "store.process",
                "egress.stop",
                "worker.parse",
                "store.state FINALIZING",
                "workspace.seal",
                "store.finish COMPLETED",
                "workspace.retire",
            ],
            msg="call order",
        )

    def test_the_report_describes_the_completed_run(self) -> None:
        """Fields come from the ports, the duration from the injected clock."""
        report = make_rig().task.run(SPEC)
        self.assertIsInstance(report, RunReport, msg="a report is returned")
        self.assertEqual(report.terminal_reason, TerminalReason.COMPLETED, msg="reason")
        self.assertEqual(report.state, RunState.TERMINAL, msg="state")
        self.assertEqual(report.run_id, RUN_ID, msg="run id")
        self.assertEqual(report.base_commit, BASE, msg="base commit")
        self.assertEqual(report.branch, RUN_ID.branch_name, msg="branch")
        self.assertEqual(report.commits_on_branch, 1, msg="commits")
        self.assertEqual(report.worker_cli_version, "2.1.304", msg="cli version")
        self.assertEqual(report.worker_exit_status, 0, msg="exit status")
        self.assertEqual(report.duration_s, 7.0, msg="duration from the injected clock")
        self.assertEqual(report.result_text, "done", msg="result text")
        self.assertEqual(report.notes, ("uncommitted changes not included",), msg="notes")
        self.assertEqual(report.survivors, (), msg="no survivors")
        self.assertIsNone(report.limit_hit, msg="no limit hit")

    def test_the_worker_gets_the_configured_limits_and_the_overrides(self) -> None:
        """Defaults apply unless the task overrides them."""
        rig = make_rig()
        rig.task.run(SPEC)
        rig.task.run(TaskSpec(repo="/work/example", task="x", hard_s=5.0, memory_bytes=99))
        self.assertEqual(
            rig.confinement.limits_seen,
            [DEFAULT_LIMITS, Limits(hard_s=5.0, memory_bytes=99)],
            msg="limits per run",
        )

    def test_the_store_sees_the_process_identity_and_the_finish_record(self) -> None:
        """The group identity is stored while the worker runs, the result at the end."""
        rig = make_rig()
        rig.task.run(SPEC)
        self.assertEqual(rig.store.identities[0].pgid, 4242, msg="identity stored")
        self.assertEqual(rig.store.begun[0].base_commit, BASE, msg="record has base commit")
        self.assertEqual(rig.store.begun[0].cli_version, "2.1.304", msg="record has version")
        self.assertEqual(rig.store.finished[0].commits_on_branch, 1, msg="finish has commits")
        self.assertIsNone(rig.store.finished[0].limit_hit, msg="finish has no limit")

    def test_a_run_without_changes_has_no_branch(self) -> None:
        """Q4: nothing sealed means no branch in the report."""
        rig = make_rig()
        rig.workspace.sealed = SealedResult(branch=None, commits_on_branch=0)
        report = rig.task.run(SPEC)
        self.assertIsNone(report.branch, msg="no branch")
        self.assertEqual(report.commits_on_branch, 0, msg="no commits")
        self.assertIn("worker made no changes", report.notes, msg="note explains it")

    def test_the_sandbox_is_removed_when_nothing_survived(self) -> None:
        """Retirement keeps nothing on the happy path."""
        rig = make_rig()
        rig.task.run(SPEC)
        self.assertEqual(rig.workspace.keep_sandbox_calls, [False], msg="sandbox removed")
