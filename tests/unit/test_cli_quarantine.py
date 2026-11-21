"""Tests of `agentsd quarantine resolve`: the re-check, the release and their exit codes."""

import json
import unittest

from agents_dispatcher.application.errors import (
    QuarantineStateError,
    ReleaseNotConfirmedError,
    UnknownRunError,
)
from agents_dispatcher.application.public_views import quarantine_payload
from agents_dispatcher.application.quarantine_resolution import Resolution, ResolutionOutcome
from agents_dispatcher.domain.run import Survivor
from tests.support.cli_book import Book, invoke
from tests.support.run_spies import RUN_ID

HELD = Resolution(
    RUN_ID,
    ResolutionOutcome.STILL_QUARANTINED,
    survivors=(Survivor(4242, 1000.5, ("group",)),),
)


def resolved(outcome: ResolutionOutcome, *warnings: str) -> Resolution:
    """Build a resolution with no survivors."""
    return Resolution(RUN_ID, outcome, warnings=warnings)


class ResolveTests(unittest.TestCase):
    """A plain re-check."""

    def test_a_run_that_settled_exits_zero_and_shows_the_shared_payload(self) -> None:
        """The JSON on standard output is the one source the views share."""
        book = Book()
        book.quarantine.result = resolved(ResolutionOutcome.SETTLED)
        outcome = invoke(["quarantine", "resolve", RUN_ID.value], book)
        self.assertEqual(outcome.code, 0, msg="settled is success")
        self.assertEqual(
            json.loads(outcome.stdout),
            quarantine_payload(book.quarantine.result),
            msg="the printed payload is the shared one",
        )
        self.assertEqual(book.quarantine.resolved, [RUN_ID], msg="and the run was resolved")

    def test_a_run_that_is_still_held_exits_three_and_names_its_survivors(self) -> None:
        """Pending, not failed: the caller must act, not retry blindly."""
        book = Book()
        book.quarantine.result = HELD
        outcome = invoke(["quarantine", "resolve", RUN_ID.value], book)
        self.assertEqual(outcome.code, 3, msg="still quarantined is exit 3")
        shown = json.loads(outcome.stdout)
        self.assertEqual(shown["outcome"], "STILL_QUARANTINED", msg="the outcome is named")
        self.assertEqual(
            shown["survivors"],
            [{"pid": 4242, "start_time": 1000.5, "sources": ["group"]}],
            msg="and so is the survivor",
        )

    def test_a_released_run_whose_look_is_still_unverified_exits_three(self) -> None:
        """The look found a process again: the sandbox stays and the caller is told."""
        book = Book()
        book.quarantine.result = Resolution(RUN_ID, ResolutionOutcome.STILL_UNVERIFIED)
        outcome = invoke(["quarantine", "resolve", RUN_ID.value], book)
        self.assertEqual(outcome.code, 3, msg="unverified is exit 3")

    def test_a_verified_release_exits_zero(self) -> None:
        """The later look showed the processes gone."""
        book = Book()
        book.quarantine.result = resolved(ResolutionOutcome.VERIFIED)
        outcome = invoke(["quarantine", "resolve", RUN_ID.value], book)
        self.assertEqual(outcome.code, 0, msg="verified is success")

    def test_a_settlement_with_a_warning_exits_one_and_still_prints_the_outcome(self) -> None:
        """A sandbox that could not be removed must not look like a clean success."""
        book = Book()
        book.quarantine.result = resolved(ResolutionOutcome.SETTLED, "sandbox kept")
        outcome = invoke(["quarantine", "resolve", RUN_ID.value], book)
        self.assertEqual(outcome.code, 1, msg="a warning is not success")
        self.assertEqual(json.loads(outcome.stdout)["warnings"], ["sandbox kept"], msg="shown")

    def test_an_unknown_run_exits_twenty_one(self) -> None:
        """The standard code of an id the store does not know."""
        book = Book()
        book.quarantine.error = UnknownRunError("no such run")
        outcome = invoke(["quarantine", "resolve", RUN_ID.value], book)
        self.assertEqual(outcome.code, 21, msg="unknown run")

    def test_a_run_in_the_wrong_state_exits_two(self) -> None:
        """Nothing to resolve is an input error."""
        book = Book()
        book.quarantine.error = QuarantineStateError("it is RUNNING")
        outcome = invoke(["quarantine", "resolve", RUN_ID.value], book)
        self.assertEqual(outcome.code, 2, msg="wrong state")
        self.assertIn("RUNNING", outcome.stderr, msg="and the reason is shown")


class ReleaseTests(unittest.TestCase):
    """The owner's release."""

    def test_release_with_the_confirmation_is_recorded_and_exits_zero(self) -> None:
        """Both flags reach the use case, with the confirmation as typed."""
        book = Book()
        book.quarantine.result = resolved(ResolutionOutcome.RELEASED)
        outcome = invoke(
            ["quarantine", "resolve", RUN_ID.value, "--release", "--confirm", RUN_ID.value], book
        )
        self.assertEqual(outcome.code, 0, msg="a recorded release is success")
        self.assertEqual(
            book.quarantine.released, [(RUN_ID, RUN_ID.value)], msg="release got the confirmation"
        )
        self.assertEqual(book.quarantine.resolved, [], msg="and no plain re-check ran")

    def test_release_without_a_confirmation_is_refused_before_any_service_call(self) -> None:
        """One flag alone must never release."""
        book = Book()
        outcome = invoke(["quarantine", "resolve", RUN_ID.value, "--release"], book)
        self.assertEqual(outcome.code, 2, msg="invalid input")
        self.assertEqual(book.factory_calls, 0, msg="no service was even built")
        self.assertEqual(book.quarantine.released, [], msg="and nothing was released")

    def test_a_confirmation_without_release_is_refused(self) -> None:
        """A stray confirmation is a mistake, not a re-check."""
        book = Book()
        outcome = invoke(["quarantine", "resolve", RUN_ID.value, "--confirm", RUN_ID.value], book)
        self.assertEqual(outcome.code, 2, msg="invalid input")
        self.assertEqual(book.quarantine.resolved, [], msg="no re-check ran")

    def test_a_wrong_confirmation_is_an_input_error(self) -> None:
        """The use case refuses it and the code says so."""
        book = Book()
        book.quarantine.error = ReleaseNotConfirmedError("repeat the run id")
        other = "11111111-2222-4333-8444-555555555555"
        outcome = invoke(
            ["quarantine", "resolve", RUN_ID.value, "--release", "--confirm", other], book
        )
        self.assertEqual(outcome.code, 2, msg="wrong confirmation")


if __name__ == "__main__":
    unittest.main()
