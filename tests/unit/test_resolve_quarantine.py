"""Tests of the resolution of a quarantined run: re-check, settle, release and verify."""

import unittest
from dataclasses import dataclass

from agents_dispatcher.application.errors import (
    DecisionLogError,
    QuarantineStateError,
    ReleaseNotConfirmedError,
    UnknownRunError,
    WorkspaceError,
)
from agents_dispatcher.application.ports import AbsenceResult, HeldRun
from agents_dispatcher.application.quarantine_resolution import (
    Resolution,
    ResolutionOutcome,
    ResolveQuarantine,
)
from agents_dispatcher.domain.routing import TaskMode
from agents_dispatcher.domain.run import RunState, TerminalReason
from tests.support.quarantine_spies import (
    ALIVE,
    GONE,
    KEPT,
    STAMP,
    SpyAmender,
    SpyEvents,
    SpyKept,
    SpyLedger,
    SpyLook,
    held_run,
)
from tests.support.run_spies import RUN_ID, Journal

VERIFIED_AT = 5000.0
OTHER_ID = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"


@dataclass
class Rig:
    """The use case over spies, plus the spies."""

    use_case: ResolveQuarantine
    journal: Journal
    ledger: SpyLedger
    look: SpyLook
    kept: SpyKept
    events: SpyEvents
    warnings: list[str]


def make_rig(run: HeldRun | None = None) -> Rig:
    """Build the use case over spies holding the default quarantined write run."""
    journal = Journal()
    ledger = SpyLedger(journal)
    ledger.runs[RUN_ID] = run if run is not None else held_run()
    ledger.kept[RUN_ID] = KEPT
    look = SpyLook(journal)
    kept = SpyKept(journal)
    events = SpyEvents()
    warnings: list[str] = []
    use_case = ResolveQuarantine(
        ledger=ledger,
        look=look,
        workspace=kept,
        amender=SpyAmender(),
        events=events,
        epoch_s=lambda: VERIFIED_AT,
        stamp=lambda _epoch: STAMP,
        warn=warnings.append,
    )
    return Rig(use_case, journal, ledger, look, kept, events, warnings)


class SettleTests(unittest.TestCase):
    """A quarantined run whose survivors are gone ends for real."""

    def test_a_run_shown_empty_is_sealed_settled_and_its_sandbox_removed_in_that_order(
        self,
    ) -> None:
        """The slot is freed only after the result is safe; the sandbox goes last."""
        rig = make_rig()
        result = rig.use_case.resolve(RUN_ID)
        self.assertEqual(result.outcome, ResolutionOutcome.SETTLED, msg="outcome")
        self.assertEqual(
            rig.journal.events,
            ["look", "kept.seal", "ledger.settle COMPLETED", "kept.remove"],
            msg="order of the effects",
        )

    def test_the_settled_record_keeps_the_workers_own_outcome_and_adds_the_result_and_the_note(
        self,
    ) -> None:
        """COMPLETED stays COMPLETED; the branch comes from the seal; the note says what is true."""
        rig = make_rig()
        rig.use_case.resolve(RUN_ID)
        record, verified_at = rig.ledger.settled[0]
        self.assertEqual(record.terminal_reason, TerminalReason.COMPLETED, msg="own outcome kept")
        self.assertEqual(
            (record.branch, record.commits_on_branch), ("agentsd-run-x", 1), msg="seal"
        )
        self.assertEqual(verified_at, VERIFIED_AT, msg="time of the verification")
        self.assertIn(
            f"quarantined, absence verified at {STAMP}, files may have changed after the "
            "worker's exit",
            record.report_json,
            msg="the note",
        )
        self.assertTrue(record.report_json.endswith("resolved=True"), msg="the problem is closed")

    def test_a_read_run_is_not_sealed(self) -> None:
        """It could not change the worktree, so there is no result branch to publish."""
        rig = make_rig(held_run(mode=TaskMode.READ))
        rig.use_case.resolve(RUN_ID)
        self.assertNotIn("kept.seal", rig.journal.events, msg="no seal for a read run")
        self.assertEqual(rig.ledger.settled[0][0].branch, None, msg="no branch")

    def test_a_failed_outcome_is_never_upgraded(self) -> None:
        """A leftover that is gone does not make a failed run succeed."""
        rig = make_rig()
        rig.ledger.kept[RUN_ID] = KEPT.__class__(
            TerminalReason.FAILED, None, 0, None, 1, 3.0, "REPORT"
        )
        rig.use_case.resolve(RUN_ID)
        self.assertEqual(
            rig.ledger.settled[0][0].terminal_reason, TerminalReason.FAILED, msg="still failed"
        )

    def test_a_live_survivor_leaves_the_run_quarantined_and_touches_nothing(self) -> None:
        """No seal, no settle, no removal: the files could still change."""
        rig = make_rig()
        rig.look.answers = [ALIVE]
        result = rig.use_case.resolve(RUN_ID)
        self.assertEqual(result.outcome, ResolutionOutcome.STILL_QUARANTINED, msg="outcome")
        self.assertEqual(result.survivors, ALIVE.survivors, msg="and the survivors are named")
        self.assertEqual(rig.journal.events, ["look"], msg="only the look happened")

    def test_a_seal_that_fails_leaves_the_run_quarantined(self) -> None:
        """The slot is not freed over a result that could not be kept."""
        rig = make_rig()
        rig.kept.seal_error = WorkspaceError("git: cannot write the tree")
        with self.assertRaises(WorkspaceError, msg="the cause reaches the caller"):
            rig.use_case.resolve(RUN_ID)
        self.assertEqual(rig.ledger.settled, [], msg="nothing was settled")
        self.assertNotIn("kept.remove", rig.journal.events, msg="and nothing was removed")

    def test_a_sandbox_that_cannot_be_removed_is_a_warning_not_an_undo(self) -> None:
        """The run is over; the leftover folder is reported."""
        rig = make_rig()
        rig.kept.remove_error = OSError("busy")
        result = rig.use_case.resolve(RUN_ID)
        self.assertEqual(result.outcome, ResolutionOutcome.SETTLED, msg="still settled")
        self.assertIn("busy", " ".join(result.warnings), msg="and the cause is shown")


class RefusalTests(unittest.TestCase):
    """What resolve refuses."""

    def test_an_unknown_run_is_refused(self) -> None:
        """Exit 21 at the command line."""
        rig = make_rig()
        rig.ledger.runs.clear()
        with self.assertRaises(UnknownRunError, msg="unknown"):
            rig.use_case.resolve(RUN_ID)

    def test_a_run_in_another_state_is_refused(self) -> None:
        """Exit 2 at the command line: there is nothing to resolve."""
        for state in (RunState.RUNNING, RunState.FINALIZING, RunState.TERMINAL):
            with self.subTest(state=state):
                rig = make_rig(held_run(state))
                with self.assertRaises(QuarantineStateError, msg="wrong state"):
                    rig.use_case.resolve(RUN_ID)
                self.assertEqual(rig.journal.events, [], msg="and nothing was looked at")


class ReleaseTests(unittest.TestCase):
    """The owner's release: the slot is freed, the files stay."""

    def test_a_confirmed_release_ends_the_run_failed_and_keeps_the_sandbox(self) -> None:
        """No look, no seal, no removal: the owner accepts the risk."""
        rig = make_rig()
        result = rig.use_case.release(RUN_ID, confirm=RUN_ID.value)
        self.assertEqual(result.outcome, ResolutionOutcome.RELEASED, msg="outcome")
        record, note, released_at = rig.ledger.released[0]
        self.assertEqual(record.terminal_reason, TerminalReason.FAILED, msg="failed, never ok")
        self.assertEqual(note, "released by operator", msg="the note")
        self.assertEqual(released_at, VERIFIED_AT, msg="time of the release")
        self.assertEqual(rig.journal.events, ["ledger.release"], msg="nothing else happened")
        self.assertTrue(record.report_json.endswith("resolved=False"), msg="the problem stays open")

    def test_the_release_is_written_to_the_log_with_the_run_and_the_time(self) -> None:
        """The owner can find out later who let the run go."""
        rig = make_rig()
        rig.use_case.release(RUN_ID, confirm=RUN_ID.value)
        self.assertEqual(len(rig.events.lines), 1, msg="one line")
        self.assertIn(RUN_ID.value, rig.events.lines[0], msg="the run")
        self.assertIn(STAMP, rig.events.lines[0], msg="the time")

    def test_a_release_without_the_matching_confirmation_does_nothing(self) -> None:
        """Another run id, or none, is not consent."""
        rig = make_rig()
        for wrong in (OTHER_ID, ""):
            with self.subTest(confirm=wrong), self.assertRaises(ReleaseNotConfirmedError):
                rig.use_case.release(RUN_ID, confirm=wrong)
        self.assertEqual(rig.ledger.released, [], msg="nothing was released")

    def test_only_a_quarantined_run_can_be_released(self) -> None:
        """A finished run has nothing to release."""
        rig = make_rig(held_run(RunState.TERMINAL))
        with self.assertRaises(QuarantineStateError, msg="wrong state"):
            rig.use_case.release(RUN_ID, confirm=RUN_ID.value)


class VerifyTests(unittest.TestCase):
    """A released run is looked at again until its processes are shown gone."""

    def released(self) -> Rig:
        """Build a rig holding a released run whose absence was never verified."""
        return make_rig(held_run(RunState.TERMINAL, released_at=4000.0))

    def test_a_released_run_shown_empty_has_its_sandbox_removed_and_is_marked(self) -> None:
        """The run stays terminal and failed; only the folder goes."""
        rig = self.released()
        result = rig.use_case.resolve(RUN_ID)
        self.assertEqual(result.outcome, ResolutionOutcome.VERIFIED, msg="outcome")
        self.assertEqual(
            rig.journal.events, ["look", "kept.remove", "ledger.verified"], msg="steps"
        )
        self.assertEqual(rig.ledger.verified, [VERIFIED_AT], msg="time of the verification")
        self.assertEqual(rig.ledger.settled, [], msg="not settled again")

    def test_a_released_run_with_a_live_survivor_keeps_its_sandbox(self) -> None:
        """Nothing is deleted under a process that may still use it."""
        rig = self.released()
        rig.look.answers = [ALIVE]
        result = rig.use_case.resolve(RUN_ID)
        self.assertEqual(result.outcome, ResolutionOutcome.STILL_UNVERIFIED, msg="outcome")
        self.assertEqual(rig.journal.events, ["look"], msg="only the look")

    def test_a_released_run_that_was_already_verified_is_refused(self) -> None:
        """Nothing is left to do."""
        rig = make_rig(held_run(RunState.TERMINAL, released_at=4000.0, verified_at=4500.0))
        with self.assertRaises(QuarantineStateError, msg="already verified"):
            rig.use_case.resolve(RUN_ID)


class RecheckAllTests(unittest.TestCase):
    """The start hook looks at every run that waits for a look; one failure stops no other."""

    def test_every_waiting_run_is_looked_at_and_a_failing_look_is_logged_not_raised(self) -> None:
        """A broken re-check never blocks a new start."""
        rig = make_rig()
        rig.ledger.waiting = [RUN_ID]
        rig.look.answers = [OSError("cannot read the process table")]
        results = rig.use_case.recheck_all()
        self.assertEqual(results, [], msg="no result for the run that could not be looked at")
        self.assertIn(
            "cannot read the process table", " ".join(rig.events.lines), msg="the error is logged"
        )

    def test_a_run_that_cannot_be_sealed_is_logged_and_the_next_one_is_still_looked_at(
        self,
    ) -> None:
        """The repository may be gone; that must not stop the start or the other runs."""
        rig = make_rig()
        rig.ledger.waiting = [RUN_ID, RUN_ID]
        rig.kept.seal_error = WorkspaceError("the repository is gone")
        results = rig.use_case.recheck_all()
        self.assertEqual(results, [], msg="nothing was settled")
        self.assertEqual(
            len([line for line in rig.events.lines if "the repository is gone" in line]),
            2,
            msg="both runs were tried and both failures logged",
        )

    def test_an_unexpected_failure_of_a_look_is_logged_too(self) -> None:
        """The hook runs inside every start; whatever goes wrong, the start goes on."""
        rig = make_rig()
        rig.ledger.waiting = [RUN_ID]
        rig.look.answers = [RuntimeError("table layout changed")]
        self.assertEqual(rig.use_case.recheck_all(), [], msg="no result")
        self.assertIn("table layout changed", " ".join(rig.events.lines), msg="and it is logged")

    def test_a_settled_run_is_reported(self) -> None:
        """The hook can tell which runs it ended."""
        rig = make_rig()
        rig.ledger.waiting = [RUN_ID]
        results: list[Resolution] = rig.use_case.recheck_all()
        self.assertEqual([item.outcome for item in results], [ResolutionOutcome.SETTLED], msg="r")
        self.assertEqual(GONE.survivors, (), msg="the canned look was empty")


class ResidualTests(unittest.TestCase):
    """A bystander that holds the sandbox keeps it, however empty the run is."""

    HOLDER = AbsenceResult(empty=True, survivors=(), residual=("pid 7",))

    def test_a_quarantined_run_whose_folder_is_held_stays_quarantined_and_untouched(self) -> None:
        """Nothing is sealed, settled or removed under a process that is standing in the folder."""
        rig = make_rig()
        rig.look.answers = [self.HOLDER]
        result = rig.use_case.resolve(RUN_ID)
        self.assertIs(result.outcome, ResolutionOutcome.STILL_QUARANTINED, msg="still held")
        self.assertIn("pid 7", " ".join(result.warnings), msg="the holder is named")
        self.assertEqual(rig.journal.events, ["look"], msg="no seal, no settlement, no removal")

    def test_a_released_run_whose_folder_is_held_stays_unverified(self) -> None:
        """The same rule on the verification path."""
        rig = make_rig(held_run(RunState.TERMINAL, released_at=4000.0))
        rig.look.answers = [self.HOLDER]
        result = rig.use_case.resolve(RUN_ID)
        self.assertIs(result.outcome, ResolutionOutcome.STILL_UNVERIFIED, msg="still unverified")
        self.assertEqual(rig.journal.events, ["look"], msg="nothing stamped, nothing removed")

    def test_once_the_holder_is_gone_the_next_look_settles_and_removes(self) -> None:
        """The hold is not permanent: the same run resolves when the folder is free."""
        rig = make_rig()
        rig.look.answers = [self.HOLDER, GONE]
        rig.use_case.resolve(RUN_ID)
        result = rig.use_case.resolve(RUN_ID)
        self.assertIs(result.outcome, ResolutionOutcome.SETTLED, msg="settled the second time")
        self.assertIn("kept.remove", rig.journal.events, msg="and removed then")


class RobustnessTests(unittest.TestCase):
    """A step that fails after the ledger changed, or a log that cannot be written."""

    def test_an_unreadable_process_seen_by_the_look_is_named_in_the_settled_report(self) -> None:
        """Settling must not erase the doubt the look carried."""
        rig = make_rig()
        rig.look.answers = [AbsenceResult(empty=True, survivors=(), unreadable=("pid 5",))]
        rig.use_case.resolve(RUN_ID)
        report = rig.ledger.settled[0][0].report_json
        self.assertIn("paths of pid 5 could not be read", report, msg="the doubt is in the report")

    def test_a_workspace_error_while_removing_after_settling_is_a_warning(self) -> None:
        """The run is already over; the leftover sandbox is reported, not thrown."""
        rig = make_rig()
        rig.kept.remove_error = WorkspaceError("git worktree prune failed")
        result = rig.use_case.resolve(RUN_ID)
        self.assertIs(result.outcome, ResolutionOutcome.SETTLED, msg="still settled")
        self.assertIn("prune failed", " ".join(result.warnings), msg="and the cause is shown")

    def test_an_unreadable_process_seen_by_the_look_is_written_to_the_log(self) -> None:
        """The re-check leaves the same trace as the end of a run does."""
        rig = make_rig()
        rig.look.answers = [AbsenceResult(empty=True, survivors=(), unreadable=("pid 5",))]
        rig.use_case.resolve(RUN_ID)
        self.assertTrue(
            any("UNREADABLE_RESIDUAL" in line and "pid 5" in line for line in rig.events.lines),
            msg="logged",
        )

    def test_a_released_run_whose_sandbox_could_not_be_removed_is_not_marked_verified(self) -> None:
        """Verification stands only when the removal worked, so a later look retries it."""
        rig = make_rig(held_run(RunState.TERMINAL, released_at=4000.0))
        rig.kept.remove_error = OSError("directory not empty")
        result = rig.use_case.resolve(RUN_ID)
        self.assertIs(result.outcome, ResolutionOutcome.STILL_UNVERIFIED, msg="not verified")
        self.assertEqual(rig.ledger.verified, [], msg="and nothing was stamped")
        self.assertIn("directory not empty", " ".join(result.warnings), msg="with the cause")

    def test_a_settlement_and_a_verification_are_written_to_the_log(self) -> None:
        """A state change with no trace is what the log exists to prevent."""
        settled = make_rig()
        settled.use_case.resolve(RUN_ID)
        self.assertTrue(
            any("QUARANTINE_SETTLED" in line for line in settled.events.lines),
            msg="settled is logged",
        )
        verified = make_rig(held_run(RunState.TERMINAL, released_at=4000.0))
        verified.use_case.resolve(RUN_ID)
        self.assertTrue(
            any("QUARANTINE_VERIFIED" in line for line in verified.events.lines),
            msg="verified is logged",
        )

    def test_an_unwritable_log_never_makes_the_recheck_raise(self) -> None:
        """The start hook must survive a full disk; the failure goes to the warning channel."""
        rig = make_rig()
        rig.ledger.waiting = [RUN_ID]
        rig.look.answers = [OSError("table unreadable")]
        rig.events.failure = DecisionLogError("disk full")
        self.assertEqual(rig.use_case.recheck_all(), [], msg="no result, no exception")
        self.assertIn("disk full", " ".join(rig.warnings), msg="the log failure is shown")

    def test_a_release_whose_log_line_failed_is_still_a_recorded_release(self) -> None:
        """The ledger is the record; the command must not report a false failure."""
        rig = make_rig()
        rig.events.failure = DecisionLogError("disk full")
        result = rig.use_case.release(RUN_ID, confirm=RUN_ID.value)
        self.assertIs(result.outcome, ResolutionOutcome.RELEASED, msg="released")
        self.assertIn("disk full", " ".join(result.warnings), msg="and the log failure is shown")


if __name__ == "__main__":
    unittest.main()
