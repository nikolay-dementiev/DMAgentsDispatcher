"""Tests of stop handling in the supervised run: before the worker starts and while it runs."""

import unittest

from agents_dispatcher.application.ports import ProcessIdentity
from agents_dispatcher.domain.run import RunState, TaskSpec, TerminalReason
from tests.support.run_spies import RUN_ID
from tests.unit.test_run_task import make_rig

SPEC = TaskSpec(repo="/work/example", task="fix the bug", hard_s=60.0, memory_bytes=1024)
SUPERVISOR = ProcessIdentity(pgid=777, start_time_s=5.5)


class StopBeforeTheWorkerTests(unittest.TestCase):
    """A stop that arrives before the worker was launched ends the run without one."""

    def test_a_stop_recorded_before_the_claim_ends_the_run_without_a_worker(self) -> None:
        """No proxy, no worker, no seal: STOPPED with a report that says why."""
        rig = make_rig()
        rig.stops.requested_now = True
        report = rig.task.supervise(SPEC, RUN_ID, SUPERVISOR)
        self.assertEqual(report.terminal_reason, TerminalReason.STOPPED, msg="stopped")
        self.assertEqual(report.state, RunState.TERMINAL, msg="terminal")
        self.assertIn("before the worker started", " ".join(report.notes), msg="the note")
        for forbidden in ("egress.start", "confinement.run", "workspace.seal"):
            self.assertNotIn(forbidden, rig.journal.events, msg=f"{forbidden} did not run")
        self.assertEqual(
            rig.store.finished[0].terminal_reason, TerminalReason.STOPPED, msg="stored"
        )
        self.assertEqual(
            rig.journal.events[-2:], ["workspace.retire", "owner.release"], msg="clean"
        )


class StopWhileRunningTests(unittest.TestCase):
    """A stop that arrives during the run is acted on by the watch loop."""

    def test_the_watch_loop_is_given_a_poll_that_reads_the_stop_request(self) -> None:
        """The first positive answer marks the run STOPPING and the outcome becomes STOPPED."""
        rig = make_rig()
        rig.stops.requested_from_call = 3  # the pre-launch check is call 1
        rig.confinement.stop_polled_true_after = 0
        report = rig.task.supervise(SPEC, RUN_ID, SUPERVISOR)
        self.assertIn("confinement.stop_seen", rig.journal.events, msg="the poll saw the stop")
        self.assertEqual(report.terminal_reason, TerminalReason.STOPPED, msg="stopped")
        self.assertEqual(rig.store.states.count(RunState.STOPPING), 1, msg="STOPPING exactly once")
        stopping = rig.journal.events.index("store.state STOPPING")
        finalizing = rig.journal.events.index("store.state FINALIZING")
        self.assertLess(stopping, finalizing, msg="STOPPING comes before FINALIZING")

    def test_a_poll_that_stays_false_changes_nothing(self) -> None:
        """No request: the run completes and STOPPING is never stored."""
        rig = make_rig()
        report = rig.task.supervise(SPEC, RUN_ID, SUPERVISOR)
        self.assertEqual(report.terminal_reason, TerminalReason.COMPLETED, msg="completed")
        self.assertNotIn(RunState.STOPPING, rig.store.states, msg="no STOPPING")

    def test_the_sealed_work_of_a_stopped_run_is_kept(self) -> None:
        """A stopped run still seals what the worker had written."""
        rig = make_rig()
        rig.stops.requested_from_call = 3
        rig.confinement.stop_polled_true_after = 0
        report = rig.task.supervise(SPEC, RUN_ID, SUPERVISOR)
        self.assertIn("workspace.seal", rig.journal.events, msg="sealed")
        self.assertEqual(report.branch, RUN_ID.branch_name, msg="result branch kept")


if __name__ == "__main__":
    unittest.main()
