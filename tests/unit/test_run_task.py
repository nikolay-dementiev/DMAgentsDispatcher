"""Tests of the RunTask use case with spies for every port."""

import unittest
from dataclasses import dataclass

from agents_dispatcher.adapters.system.json_report import JsonReportRenderer
from agents_dispatcher.application.errors import LaunchError, WorkspaceNotGitError
from agents_dispatcher.application.ports import (
    EgressEndpoint,
    ExecutionOutcome,
    SealedResult,
    WorkerOutcome,
)
from agents_dispatcher.application.run_task import RunTask
from agents_dispatcher.domain.run import (
    LimitKind,
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
    SpyOwners,
    SpyStops,
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
    stops: SpyStops


def make_rig() -> Rig:
    """Build a use case where every port is a spy sharing one journal."""
    journal = Journal()
    clock = FakeClock(start=100.0)
    workspace = SpyWorkspace(journal)
    egress = SpyEgress(journal)
    worker = SpyWorker(journal)
    confinement = SpyConfinement(journal, clock)
    store = SpyStore(journal)
    stops = SpyStops(journal)
    task = RunTask(
        workspace=workspace,
        egress=egress,
        worker=worker,
        confinement=confinement,
        store=store,
        clock=clock,
        ids=SpyIds(journal),
        owners=SpyOwners(journal),
        stops=stops,
        renderer=JsonReportRenderer(),
        default_limits=DEFAULT_LIMITS,
    )
    return Rig(task, journal, workspace, egress, worker, confinement, store, stops)


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
                "owner.hold",
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
                "owner.release",
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

    def test_the_confinement_is_told_where_the_proxy_listens(self) -> None:
        """The profile opens exactly the proxy port, so the endpoint must reach the confinement."""
        rig = make_rig()
        rig.task.run(SPEC)
        self.assertEqual(
            rig.confinement.egress_seen,
            [EgressEndpoint(host="127.0.0.1", port=40123)],
            msg="the endpoint the proxy returned",
        )

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

    def test_proxy_faults_reach_the_report_notes(self) -> None:
        """A listener that failed during the run is named in the report, not only in a log."""
        rig = make_rig()
        rig.egress.canned_faults = ("accept failed: too many open files",)
        report = rig.task.run(SPEC)
        self.assertIn(
            "proxy: accept failed: too many open files", report.notes, msg="the fault is a note"
        )

    def test_a_clean_proxy_adds_no_note(self) -> None:
        """No fault, no note."""
        report = make_rig().task.run(SPEC)
        self.assertEqual(report.notes, ("uncommitted changes not included",), msg="notes unchanged")

    def test_the_sandbox_is_removed_when_nothing_survived(self) -> None:
        """Retirement keeps nothing on the happy path."""
        rig = make_rig()
        rig.task.run(SPEC)
        self.assertEqual(rig.workspace.keep_sandbox_calls, [False], msg="sandbox removed")


class FailurePathTests(unittest.TestCase):
    """Runs that do not complete: each gets its own terminal reason."""

    def test_a_failed_worker_gives_failed(self) -> None:
        """The worker adapter says the run did not succeed."""
        rig = make_rig()
        rig.worker.outcome = WorkerOutcome(succeeded=False, result_text="boom")
        report = rig.task.run(SPEC)
        self.assertEqual(report.terminal_reason, TerminalReason.FAILED, msg="reason")
        self.assertEqual(report.result_text, "boom", msg="text is kept")
        self.assertEqual(rig.store.finished[0].terminal_reason, TerminalReason.FAILED, msg="stored")

    def test_a_limit_hit_gives_resource_limit(self) -> None:
        """A watchdog kill for time or memory is RESOURCE_LIMIT and names the limit."""
        rig = make_rig()
        rig.confinement.outcome = ExecutionOutcome(
            exit_status=None, stdout="", limit_hit=LimitKind.TIME
        )
        report = rig.task.run(SPEC)
        self.assertEqual(report.terminal_reason, TerminalReason.RESOURCE_LIMIT, msg="reason")
        self.assertEqual(report.limit_hit, LimitKind.TIME, msg="limit named")

    def test_a_stop_request_gives_stopped(self) -> None:
        """A stopped run is STOPPED, not FAILED."""
        rig = make_rig()
        rig.confinement.outcome = ExecutionOutcome(exit_status=None, stdout="", stopped=True)
        report = rig.task.run(SPEC)
        self.assertEqual(report.terminal_reason, TerminalReason.STOPPED, msg="reason")

    def test_a_lost_launch_gives_launch_lost_and_cleans_up(self) -> None:
        """The worker never started: LAUNCH_LOST, proxy stopped, sandbox retired."""
        rig = make_rig()
        rig.confinement.launch_error = LaunchError("no sandbox-exec")
        report = rig.task.run(SPEC)
        self.assertEqual(report.terminal_reason, TerminalReason.LAUNCH_LOST, msg="reason")
        self.assertIsNone(report.worker_exit_status, msg="no exit status")
        self.assertIn("no sandbox-exec", " ".join(report.notes), msg="cause is in the notes")
        self.assertIn("egress.stop", rig.journal.events, msg="proxy stopped")
        self.assertIn("workspace.retire", rig.journal.events, msg="workspace retired")

    def test_surviving_processes_fail_the_run_and_keep_the_sandbox(self) -> None:
        """A worker process that outlived the sweep must not be hidden by a clean retire."""
        rig = make_rig()
        rig.confinement.outcome = ExecutionOutcome(
            exit_status=0, stdout="{}", survivors=("pid 9",), survivors_alive=("pid 9",)
        )
        report = rig.task.run(SPEC)
        self.assertEqual(report.terminal_reason, TerminalReason.FAILED, msg="reason")
        self.assertEqual(report.survivors, ("pid 9",), msg="survivors reported")
        self.assertEqual(rig.workspace.keep_sandbox_calls, [True], msg="sandbox kept")

    def test_nothing_is_sealed_while_a_worker_process_is_still_alive(self) -> None:
        """M1: a live process could still swap files under the supervisor while it reads them."""
        rig = make_rig()
        rig.confinement.outcome = ExecutionOutcome(
            exit_status=0, stdout="{}", survivors=("pid 9",), survivors_alive=("pid 9",)
        )
        report = rig.task.run(SPEC)
        self.assertNotIn("workspace.seal", rig.journal.events, msg="the seal was skipped")
        self.assertIsNone(report.branch, msg="no result branch")
        self.assertIn("not sealed", " ".join(report.notes), msg="the note says why")

    def test_the_owner_lock_is_released_when_the_run_fails_unexpectedly(self) -> None:
        """A crash inside the use case must not leave the run looking owned."""
        rig = make_rig()

        def broken() -> str:
            message = "cannot read the CLI version"
            raise RuntimeError(message)

        rig.worker.cli_version = broken  # type: ignore[method-assign]
        with self.assertRaises(RuntimeError):
            rig.task.run(SPEC)
        self.assertEqual(
            rig.journal.events[-2:], ["workspace.retire", "owner.release"], msg="order"
        )

    def test_a_non_git_repository_is_refused_before_anything_starts(self) -> None:
        """Nothing is stored and no proxy starts."""
        rig = make_rig()
        rig.workspace.not_git = True
        with self.assertRaises(WorkspaceNotGitError):
            rig.task.run(SPEC)
        self.assertEqual(
            rig.journal.events, ["ids.new", "workspace.prepare"], msg="no other port was touched"
        )
