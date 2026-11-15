"""Tests of the account watch: the look before the launch and the look after the run."""
# ruff: noqa: ARG002 - fakes must match the port signatures even where they ignore an argument

import unittest

from agents_dispatcher.application.account_watch import ProviderAccountWatch
from agents_dispatcher.domain.account import AccountReading
from agents_dispatcher.domain.login import LoginObservation, LoginState
from agents_dispatcher.domain.quota import EvidenceKind, QuotaEvidence
from agents_dispatcher.domain.routing import (
    Disposition,
    Evaluation,
    ProblemCode,
    RunRouting,
    TaskMode,
)
from tests.support.run_spies import RUN_ID


class FakeLogin:
    """Answers the states it is given, one per look."""

    def __init__(self, *states: LoginState, note: str = "") -> None:
        """Keep the states and the note every look carries."""
        self.states = list(states)
        self.note = note

    def observe(self) -> LoginObservation:
        """Return the next state; the last one repeats."""
        state = self.states.pop(0) if len(self.states) > 1 else self.states[0]
        return LoginObservation(state, self.note)


class FakeAccount:
    """Answers an identity and counts its reads."""

    def __init__(self, identity: str | None) -> None:
        """Keep the identity."""
        self.identity = identity
        self.reads = 0

    def read(self) -> AccountReading:
        """Return a reading with the identity."""
        self.reads += 1
        return AccountReading(
            LoginState.LOGGED_IN, self.identity, QuotaEvidence(EvidenceKind.UNAVAILABLE)
        )


class FakeLedger:
    """Marks and observations in memory."""

    def __init__(self, *, lost: bool = False) -> None:
        """Start with or without a recorded loss."""
        self.lost = lost
        self.recorded: dict[str, dict[str, object]] = {}

    def is_lost(self, provider: str) -> bool:
        """Return whether a loss is recorded."""
        return self.lost

    def mark_lost(self, provider: str) -> None:
        """Record a loss."""
        self.lost = True

    def clear_lost(self, provider: str) -> None:
        """Forget the loss."""
        self.lost = False

    def record_observation(self, run_id: object, phase: str, facts: dict[str, object]) -> None:
        """Keep the facts of a phase."""
        self.recorded[phase] = facts

    def observations(self, run_id: object) -> dict[str, dict[str, object]]:
        """Return what was recorded."""
        return self.recorded


def routing(fingerprint: str | None) -> RunRouting:
    """Return the routing of a run whose admission showed `fingerprint` (None: unknown)."""
    evidence: dict[str, object] = {"account": {"fingerprint": fingerprint}} if fingerprint else {}
    selected = Evaluation("openai", Disposition.SELECTED, model="m", effort="e", evidence=evidence)
    return RunRouting(None, TaskMode.READ, "openai", "m", "e", (selected,))


def make_watch(
    login: FakeLogin, account: FakeAccount | None, ledger: FakeLedger
) -> ProviderAccountWatch:
    """Build a watch over fakes; a fingerprint is the id with a prefix."""
    return ProviderAccountWatch(
        login=login,
        account=account,
        ledger=ledger,
        fingerprint=lambda identity: f"fp-{identity}",
    )


class BeforeLaunchTests(unittest.TestCase):
    """The look just before the launch."""

    def test_logged_out_refuses_the_launch(self) -> None:
        """AUTH_REQUIRED, and the look is recorded."""
        ledger = FakeLedger()
        refusal = make_watch(FakeLogin(LoginState.LOGGED_OUT), None, ledger).before_launch(
            RUN_ID, routing(None)
        )
        assert refusal is not None  # noqa: S101 - narrows the type
        self.assertEqual(refusal.code, ProblemCode.AUTH_REQUIRED, msg="auth required")
        self.assertEqual(ledger.recorded["pre_launch"]["login"], "LOGGED_OUT", msg="recorded")

    def test_logged_in_and_unknown_launch(self) -> None:
        """A look that cannot be made refuses nothing."""
        for state in (LoginState.LOGGED_IN, LoginState.UNKNOWN):
            ledger = FakeLedger()
            refusal = make_watch(FakeLogin(state), None, ledger).before_launch(
                RUN_ID, routing(None)
            )
            self.assertIsNone(refusal, msg=f"{state.name} launches")
            self.assertEqual(ledger.recorded["pre_launch"]["login"], state.name, msg="recorded")

    def test_a_known_account_that_changed_refuses_the_launch(self) -> None:
        """Admission showed fp-a; now the account is b."""
        refusal = make_watch(
            FakeLogin(LoginState.LOGGED_IN), FakeAccount("b"), FakeLedger()
        ).before_launch(RUN_ID, routing("fp-a"))
        assert refusal is not None  # noqa: S101 - narrows the type
        self.assertEqual(refusal.code, ProblemCode.ACCOUNT_CHANGED, msg="account changed")

    def test_the_same_account_launches(self) -> None:
        """Equal fingerprints."""
        refusal = make_watch(
            FakeLogin(LoginState.LOGGED_IN), FakeAccount("a"), FakeLedger()
        ).before_launch(RUN_ID, routing("fp-a"))
        self.assertIsNone(refusal, msg="same account")

    def test_an_unknown_identity_on_either_side_is_not_a_change(self) -> None:
        """Only a KNOWN change refuses; and an unknown admission asks nothing."""
        now_unknown = FakeAccount(None)
        refusal = make_watch(
            FakeLogin(LoginState.LOGGED_IN), now_unknown, FakeLedger()
        ).before_launch(RUN_ID, routing("fp-a"))
        self.assertIsNone(refusal, msg="unknown now")
        asked = FakeAccount("b")
        refusal = make_watch(FakeLogin(LoginState.LOGGED_IN), asked, FakeLedger()).before_launch(
            RUN_ID, routing(None)
        )
        self.assertIsNone(refusal, msg="unknown at admission")
        self.assertEqual(asked.reads, 0, msg="no account read when admission knew no account")

    def test_a_recorded_loss_refuses_a_launch_that_does_not_see_a_login(self) -> None:
        """LOGIN_LOST until a login is seen."""
        refusal = make_watch(
            FakeLogin(LoginState.UNKNOWN), None, FakeLedger(lost=True)
        ).before_launch(RUN_ID, routing(None))
        assert refusal is not None  # noqa: S101 - narrows the type
        self.assertEqual(refusal.code, ProblemCode.LOGIN_LOST, msg="login lost")


class AfterRunTests(unittest.TestCase):
    """The look after the run."""

    def test_a_login_that_went_out_during_the_run_is_recorded_as_lost(self) -> None:
        """In before, out after: LOGIN_LOST mark and a note."""
        ledger = FakeLedger()
        watch = make_watch(FakeLogin(LoginState.LOGGED_IN, LoginState.LOGGED_OUT), None, ledger)
        watch.before_launch(RUN_ID, routing(None))
        notes = watch.after_run(RUN_ID, routing(None))
        self.assertTrue(ledger.lost, msg="the loss is marked")
        self.assertTrue(any("lost" in note for note in notes), msg="told in the notes")
        self.assertEqual(ledger.recorded["post_run"]["login"], "LOGGED_OUT", msg="recorded")

    def test_unknown_after_a_login_is_not_a_loss_but_is_told(self) -> None:
        """Only a seen LOGGED_OUT counts; a look that failed is still visible."""
        ledger = FakeLedger()
        watch = make_watch(
            FakeLogin(LoginState.LOGGED_IN, LoginState.UNKNOWN, note="timed out"), None, ledger
        )
        watch.before_launch(RUN_ID, routing(None))
        notes = watch.after_run(RUN_ID, routing(None))
        self.assertEqual(len(notes), 1, msg="one note")
        self.assertIn("could not be observed after the run", notes[0], msg="what happened")
        self.assertIn("timed out", notes[0], msg="why")
        self.assertFalse(ledger.lost, msg="no mark")
        self.assertEqual(ledger.recorded["post_run"]["note"], "timed out", msg="cause kept")

    def test_a_logged_out_start_is_not_a_loss(self) -> None:
        """Out before, out after: the run was never logged in."""
        ledger = FakeLedger()
        watch = make_watch(FakeLogin(LoginState.UNKNOWN, LoginState.LOGGED_OUT), None, ledger)
        watch.before_launch(RUN_ID, routing(None))
        watch.after_run(RUN_ID, routing(None))
        self.assertFalse(ledger.lost, msg="no mark")

    def test_a_login_seen_after_the_run_clears_a_recorded_loss(self) -> None:
        """A fresh LOGGED_IN clears."""
        ledger = FakeLedger(lost=True)
        watch = make_watch(FakeLogin(LoginState.LOGGED_IN), None, ledger)
        watch.after_run(RUN_ID, routing(None))
        self.assertFalse(ledger.lost, msg="cleared")


if __name__ == "__main__":
    unittest.main()
