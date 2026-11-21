"""Tests of the resolution of a quarantined run: re-check, settle, release and verify."""

import unittest
from dataclasses import dataclass

from agents_dispatcher.application.errors import (
    QuarantineStateError,
    ReleaseNotConfirmedError,
    UnknownRunError,
    WorkspaceError,
)
from agents_dispatcher.application.ports import HeldRun
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


def make_rig(run: HeldRun | None = None) -> Rig:
    """Build the use case over spies holding the default quarantined write run."""
    journal = Journal()
    ledger = SpyLedger(journal)
    ledger.runs[RUN_ID] = run if run is not None else held_run()
    ledger.kept[RUN_ID] = KEPT
    look = SpyLook(journal)
    kept = SpyKept(journal)
    events = SpyEvents()
    use_case = ResolveQuarantine(
        ledger=ledger,
        look=look,
        workspace=kept,
        amender=SpyAmender(),
        events=events,
        epoch_s=lambda: VERIFIED_AT,
        stamp=lambda _epoch: STAMP,
    )
    return Rig(use_case, journal, ledger, look, kept, events)


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

    def test_a_settled_run_is_reported(self) -> None:
        """The hook can tell which runs it ended."""
        rig = make_rig()
        rig.ledger.waiting = [RUN_ID]
        results: list[Resolution] = rig.use_case.recheck_all()
        self.assertEqual([item.outcome for item in results], [ResolutionOutcome.SETTLED], msg="r")
        self.assertEqual(GONE.survivors, (), msg="the canned look was empty")


if __name__ == "__main__":
    unittest.main()
