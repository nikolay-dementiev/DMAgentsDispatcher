"""Tests of the M2 side of the run store: accepting, claiming, stopping and reading runs."""

import tempfile
import threading
import unittest
from pathlib import Path

from agents_dispatcher.adapters.system.sqlite_run_store import SqliteRunStore
from agents_dispatcher.application.ports import (
    AcceptedRun,
    FinishRecord,
    ProcessIdentity,
)
from agents_dispatcher.domain.run import Limits, RunState, TerminalReason
from agents_dispatcher.domain.views import StopDisposition, StopInfo
from tests.support.run_spies import RUN_ID

ACCEPTED = AcceptedRun(
    run_id=RUN_ID,
    repo="/work/example",
    task="fix the bug",
    limits=Limits(hard_s=60.0, memory_bytes=1024),
)
SUPERVISOR = ProcessIdentity(pgid=4321, start_time_s=55.5)
DONE = FinishRecord(
    terminal_reason=TerminalReason.COMPLETED,
    branch=RUN_ID.branch_name,
    commits_on_branch=1,
    limit_hit=None,
    worker_exit_status=0,
    duration_s=3.0,
    report_json='{"state": "TERMINAL"}',
)


class StoreV2Case(unittest.TestCase):
    """A temporary store whose wall clock advances one second per reading."""

    def setUp(self) -> None:
        """Create the directory and the store."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = str(Path(directory.name) / "state" / "runs.sqlite3")
        self.store = self.open_store()

    def open_store(self) -> SqliteRunStore:
        """Open a store on the temporary file."""
        counter = iter(range(10_000))
        return SqliteRunStore(self.path, epoch_s=lambda: 1000.0 + next(counter), busy_s=2.0)


class AcceptAndClaimTests(StoreV2Case):
    """The start of a run."""

    def test_an_accepted_run_is_readable_with_its_task_and_limits(self) -> None:
        """Accept stores what `start` knows and nothing the supervisor will learn later."""
        self.store.accept(ACCEPTED)
        found = self.store.find(RUN_ID)
        stored = self.store.get(RUN_ID)
        self.assertIsNotNone(found, msg="the run is found")
        self.assertEqual(stored.state, RunState.ACCEPTED, msg="state")
        self.assertEqual(stored.task, "fix the bug", msg="task")
        self.assertEqual(stored.limits, Limits(60.0, 1024), msg="limits")
        self.assertEqual(stored.branch, RUN_ID.branch_name, msg="branch name known at accept")
        self.assertIsNone(stored.base_commit, msg="no base commit before the worktree")
        self.assertIsNone(stored.cli_version, msg="no CLI version before the supervisor")
        self.assertEqual(stored.revision, 1, msg="first revision")

    def test_an_unknown_run_is_not_found(self) -> None:
        """None, not an error: the read side maps it to exit 21."""
        self.assertIsNone(self.store.find(RUN_ID), msg="unknown")

    def test_claim_moves_to_starting_and_records_the_supervisor(self) -> None:
        """The acknowledgement `start` waits for."""
        self.store.accept(ACCEPTED)
        self.assertTrue(self.store.claim(RUN_ID, SUPERVISOR), msg="claimed")
        stored = self.store.get(RUN_ID)
        self.assertEqual(stored.state, RunState.STARTING, msg="state")
        self.assertEqual(stored.supervisor_pid, 4321, msg="pid")
        self.assertEqual(stored.supervisor_start_time, 55.5, msg="start time")

    def test_a_run_is_claimed_once_and_never_after_it_ended(self) -> None:
        """A late supervisor learns it lost the run."""
        self.store.accept(ACCEPTED)
        self.assertTrue(self.store.claim(RUN_ID, SUPERVISOR), msg="first claim")
        self.assertFalse(self.store.claim(RUN_ID, SUPERVISOR), msg="second claim")
        self.store.finish(RUN_ID, DONE)
        self.assertFalse(self.store.claim(RUN_ID, SUPERVISOR), msg="claim after the end")

    def test_workspace_facts_are_recorded_by_the_supervisor(self) -> None:
        """Base commit and CLI version arrive after the worktree exists."""
        self.store.accept(ACCEPTED)
        self.store.record_workspace(RUN_ID, base_commit="b" * 40, cli_version="2.1.304")
        stored = self.store.get(RUN_ID)
        self.assertEqual(stored.base_commit, "b" * 40, msg="base commit")
        self.assertEqual(stored.cli_version, "2.1.304", msg="cli version")

    def test_every_durable_change_raises_the_revision_by_one(self) -> None:
        """A caller can see that something changed."""
        self.store.accept(ACCEPTED)
        seen = [self.store.get(RUN_ID).revision]
        self.store.claim(RUN_ID, SUPERVISOR)
        seen.append(self.store.get(RUN_ID).revision)
        self.store.set_state(RUN_ID, RunState.RUNNING)
        seen.append(self.store.get(RUN_ID).revision)
        self.store.request_stop(RUN_ID, "enough")
        seen.append(self.store.get(RUN_ID).revision)
        self.store.finish(RUN_ID, DONE)
        seen.append(self.store.get(RUN_ID).revision)
        self.assertEqual(seen, [1, 2, 3, 4, 5], msg="one step per change")


class StopRequestTests(StoreV2Case):
    """Recording a stop request atomically with the state check."""

    def test_a_request_in_a_live_state_is_effective_and_recorded(self) -> None:
        """Reason and time are kept; the snapshot shows them."""
        for state in (RunState.ACCEPTED, RunState.STARTING, RunState.RUNNING):
            with self.subTest(state=state.name):
                self.setUp()
                self.store.accept(ACCEPTED)
                if state is not RunState.ACCEPTED:
                    self.store.claim(RUN_ID, SUPERVISOR)
                if state is RunState.RUNNING:
                    self.store.set_state(RUN_ID, RunState.RUNNING)
                answer = self.store.request_stop(RUN_ID, "enough")
                self.assertEqual(answer, StopDisposition.EFFECTIVE, msg="effective")
                found = self.store.find(RUN_ID)
                self.assertIsNotNone(found, msg="found")
                stop = found.stop if found else None
                self.assertIsInstance(stop, StopInfo, msg="request recorded")
                self.assertEqual(stop.reason if stop else "", "enough", msg="reason")

    def test_a_second_request_is_already_stopping_and_keeps_the_first_reason(self) -> None:
        """The first reason wins."""
        self.store.accept(ACCEPTED)
        self.store.request_stop(RUN_ID, "first")
        self.assertEqual(
            self.store.request_stop(RUN_ID, "second"),
            StopDisposition.ALREADY_STOPPING,
            msg="repeat",
        )
        found = self.store.find(RUN_ID)
        self.assertEqual(found.stop.reason if found and found.stop else "", "first", msg="kept")

    def test_finalizing_and_terminal_runs_record_nothing(self) -> None:
        """Too late and finished runs keep their rows unchanged."""
        self.store.accept(ACCEPTED)
        self.store.claim(RUN_ID, SUPERVISOR)
        self.store.set_state(RUN_ID, RunState.FINALIZING)
        before = self.store.get(RUN_ID).revision
        self.assertEqual(self.store.request_stop(RUN_ID, "x"), StopDisposition.TOO_LATE, msg="late")
        self.store.finish(RUN_ID, DONE)
        end = self.store.get(RUN_ID).revision
        self.assertEqual(
            self.store.request_stop(RUN_ID, "x"), StopDisposition.TERMINAL, msg="finished"
        )
        self.assertEqual(self.store.get(RUN_ID).revision, end, msg="no change after the end")
        self.assertEqual(end, before + 1, msg="only the finish changed the row")
        self.assertIsNone(self.store.get(RUN_ID).stop, msg="no request recorded")

    def test_an_unknown_run_answers_none(self) -> None:
        """The use case turns this into an error."""
        self.assertIsNone(self.store.request_stop(RUN_ID, "x"), msg="unknown")

    def test_concurrent_requests_make_exactly_one_effective(self) -> None:
        """The check and the write are one transaction."""
        self.store.accept(ACCEPTED)
        answers: list[StopDisposition | None] = []
        gate = threading.Barrier(2)

        def ask(reason: str) -> None:
            store = self.open_store()
            gate.wait()
            answers.append(store.request_stop(RUN_ID, reason))

        threads = [threading.Thread(target=ask, args=(name,)) for name in ("a", "b")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        self.assertCountEqual(
            answers,
            [StopDisposition.EFFECTIVE, StopDisposition.ALREADY_STOPPING],
            msg="one winner",
        )


class FinishTests(StoreV2Case):
    """The terminal write carries the report."""

    def test_finish_stores_the_report_text_with_the_terminal_state(self) -> None:
        """Report and TERMINAL are one write."""
        self.store.accept(ACCEPTED)
        self.store.finish(RUN_ID, DONE)
        found = self.store.find(RUN_ID)
        self.assertEqual(found.report_json if found else None, '{"state": "TERMINAL"}', msg="text")
        self.assertEqual(found.state if found else None, RunState.TERMINAL, msg="state")
        self.assertEqual(found.branch if found else None, RUN_ID.branch_name, msg="result branch")

    def test_an_unclaimed_run_can_be_closed_but_a_claimed_one_cannot(self) -> None:
        """Only `start` closes a run nobody took over, and only while it is still ACCEPTED."""
        self.store.accept(ACCEPTED)
        lost = FinishRecord(TerminalReason.LAUNCH_LOST, None, 0, None, None, 0.0, "{}")
        self.assertTrue(self.store.finish_unclaimed(RUN_ID, lost), msg="closed while ACCEPTED")
        self.assertEqual(
            self.store.get(RUN_ID).terminal_reason, TerminalReason.LAUNCH_LOST, msg="reason"
        )
        self.setUp()
        self.store.accept(ACCEPTED)
        self.store.claim(RUN_ID, SUPERVISOR)
        self.assertFalse(self.store.finish_unclaimed(RUN_ID, lost), msg="refused once claimed")
        self.assertEqual(self.store.get(RUN_ID).state, RunState.STARTING, msg="left alone")


if __name__ == "__main__":
    unittest.main()
