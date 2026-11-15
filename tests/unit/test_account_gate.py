"""Tests of the account gate: login, then quota, with the evidence a route shows."""

import unittest

from agents_dispatcher.application.account_gates import ProviderAccountGate
from agents_dispatcher.application.routing import GateResult
from agents_dispatcher.domain.account import AccountReading
from agents_dispatcher.domain.login import LoginObservation, LoginState
from agents_dispatcher.domain.quota import (
    EvidenceKind,
    QuotaEvidence,
    QuotaMode,
    QuotaWindow,
)
from agents_dispatcher.domain.routing import (
    Chain,
    ProblemCode,
    ProviderLink,
    ResolvedTask,
    TaskMode,
)

TASK = ResolvedTask(None, TaskMode.READ, Chain((ProviderLink("openai"),)))
ACCOUNT_ID = "acct-0001"


class FakeLogin:
    """A login observer that answers a canned observation and counts its looks."""

    def __init__(self, state: LoginState, note: str = "") -> None:
        """Keep the answer."""
        self.observation = LoginObservation(state, note)
        self.looks = 0

    def observe(self) -> LoginObservation:
        """Return the answer."""
        self.looks += 1
        return self.observation


class FakeAccount:
    """An account reader that answers a canned reading and counts its reads."""

    def __init__(self, used: float = 10.0, identity: str | None = ACCOUNT_ID) -> None:
        """Keep a snapshot with one window."""
        snapshot = QuotaEvidence(EvidenceKind.SNAPSHOT, (QuotaWindow(300, used),), age_s=0.0)
        self.reading = AccountReading(LoginState.LOGGED_IN, identity, snapshot)
        self.reads = 0

    def read(self) -> AccountReading:
        """Return the reading."""
        self.reads += 1
        return self.reading


class FakeMarks:
    """Recorded login losses."""

    def __init__(self, *, lost: bool = False) -> None:
        """Start with or without a recorded loss."""
        self.lost = lost
        self.cleared: list[str] = []
        self.asked: list[str] = []

    def is_lost(self, provider: str) -> bool:
        """Return whether a loss is recorded."""
        self.asked.append(provider)
        return self.lost

    def clear_lost(self, provider: str) -> None:
        """Forget the loss."""
        self.lost = False
        self.cleared.append(provider)


def make_gate(
    login: FakeLogin,
    account: FakeAccount | None,
    marks: FakeMarks | None = None,
    mode: QuotaMode = QuotaMode.BEST_EFFORT,
) -> ProviderAccountGate:
    """Build a gate over fakes with a reserve of 10 percent and a 60 s age bound."""
    return ProviderAccountGate(
        login=login,
        account=account,
        marks=marks or FakeMarks(),
        mode=mode,
        reserve_percent=10,
        max_age_s=60.0,
        fingerprint=lambda identity: f"fp-{len(identity)}",
    )


def codes(result: GateResult) -> list[ProblemCode]:
    """Return the codes of the refusals."""
    return [problem.code for problem in result.problems]


class LoginTests(unittest.TestCase):
    """The login comes first and decides before quota is read."""

    def test_a_logged_in_account_with_room_is_admitted_with_evidence(self) -> None:
        """No problem; login and quota evidence are shown."""
        result = make_gate(FakeLogin(LoginState.LOGGED_IN), FakeAccount()).assess("openai", TASK)
        self.assertEqual(result.problems, (), msg="admitted")
        self.assertEqual(result.warnings, (), msg="no warning")
        self.assertEqual(result.evidence["login"], {"state": "LOGGED_IN"}, msg="login shown")
        quota = result.evidence["quota"]
        assert isinstance(quota, dict)  # noqa: S101 - narrows the type for the test
        self.assertEqual(quota["result"], "ADMITTED", msg="quota admitted")

    def test_logged_out_refuses_and_quota_is_not_read(self) -> None:
        """AUTH_REQUIRED; the app-server is not asked."""
        account = FakeAccount()
        result = make_gate(FakeLogin(LoginState.LOGGED_OUT), account).assess("openai", TASK)
        self.assertEqual(codes(result), [ProblemCode.AUTH_REQUIRED], msg="auth required")
        self.assertEqual(account.reads, 0, msg="no quota read after a login refusal")
        self.assertNotIn("quota", result.evidence, msg="no quota evidence")

    def test_unknown_login_refuses_nothing_and_quota_is_still_judged(self) -> None:
        """An observation that could not be made is not a refusal."""
        account = FakeAccount()
        result = make_gate(FakeLogin(LoginState.UNKNOWN, "no look"), account).assess("openai", TASK)
        self.assertEqual(result.problems, (), msg="admitted")
        self.assertEqual(account.reads, 1, msg="quota read")
        self.assertEqual(
            result.evidence["login"], {"state": "UNKNOWN", "note": "no look"}, msg="note kept"
        )

    def test_a_recorded_loss_refuses_until_a_login_is_seen(self) -> None:
        """LOGIN_LOST for logged-out and unknown; a seen login clears it."""
        for state in (LoginState.LOGGED_OUT, LoginState.UNKNOWN):
            marks = FakeMarks(lost=True)
            result = make_gate(FakeLogin(state), FakeAccount(), marks).assess("openai", TASK)
            self.assertEqual(codes(result), [ProblemCode.LOGIN_LOST], msg=f"lost, {state.name}")
            self.assertTrue(marks.lost, msg=f"the loss stays on {state.name}")
        marks = FakeMarks(lost=True)
        result = make_gate(FakeLogin(LoginState.LOGGED_IN), FakeAccount(), marks).assess(
            "openai", TASK
        )
        self.assertEqual(result.problems, (), msg="admitted after a fresh login")
        self.assertFalse(marks.lost, msg="the loss is cleared")
        self.assertEqual(marks.cleared, ["openai"], msg="cleared for the provider asked")

    def test_a_login_with_no_recorded_loss_clears_nothing(self) -> None:
        """No write when there is nothing to clear."""
        marks = FakeMarks()
        make_gate(FakeLogin(LoginState.LOGGED_IN), FakeAccount(), marks).assess("openai", TASK)
        self.assertEqual(marks.cleared, [], msg="nothing cleared")


class QuotaTests(unittest.TestCase):
    """Quota is judged by the pure rules; the gate only feeds them."""

    def test_a_window_over_the_reserve_refuses(self) -> None:
        """QUOTA_RESERVE with the evidence kept."""
        result = make_gate(FakeLogin(LoginState.LOGGED_IN), FakeAccount(used=95.0)).assess(
            "openai", TASK
        )
        self.assertEqual(codes(result), [ProblemCode.QUOTA_RESERVE], msg="reserve refusal")
        self.assertIn("login", result.evidence, msg="login evidence kept on a refusal")

    def test_mode_off_reads_nothing(self) -> None:
        """No dialogue is run when quota is off."""
        account = FakeAccount(used=99.0)
        result = make_gate(FakeLogin(LoginState.LOGGED_IN), account, mode=QuotaMode.OFF).assess(
            "openai", TASK
        )
        self.assertEqual(result.problems, (), msg="admitted")
        self.assertEqual(account.reads, 0, msg="no read")
        quota = result.evidence["quota"]
        assert isinstance(quota, dict)  # noqa: S101 - narrows the type for the test
        self.assertEqual(quota["result"], "SKIPPED", msg="skipped")

    def test_a_provider_without_a_quota_interface_is_judged_unsupported(self) -> None:
        """Warning in best_effort, refusal in required."""
        best = make_gate(FakeLogin(LoginState.LOGGED_IN), None).assess("anthropic", TASK)
        self.assertEqual(
            [warning.code for warning in best.warnings],
            [ProblemCode.QUOTA_UNSUPPORTED_ALLOWED],
            msg="warned",
        )
        strict = make_gate(FakeLogin(LoginState.LOGGED_IN), None, mode=QuotaMode.REQUIRED).assess(
            "anthropic", TASK
        )
        self.assertEqual(codes(strict), [ProblemCode.QUOTA_UNSUPPORTED], msg="refused")

    def test_unreadable_quota_warns_in_best_effort(self) -> None:
        """Evidence that is unavailable is a warning, not a refusal, in best_effort."""
        account = FakeAccount()
        account.reading = AccountReading(
            LoginState.LOGGED_IN, None, QuotaEvidence(EvidenceKind.UNAVAILABLE, detail="down")
        )
        result = make_gate(FakeLogin(LoginState.LOGGED_IN), account).assess("openai", TASK)
        self.assertEqual(
            [warning.code for warning in result.warnings],
            [ProblemCode.QUOTA_UNKNOWN_ALLOWED],
            msg="warned",
        )


class IdentityTests(unittest.TestCase):
    """The account id is compared, never shown; its fingerprint is."""

    def test_a_known_identity_is_shown_only_as_a_fingerprint(self) -> None:
        """The route carries the fingerprint, not the id."""
        result = make_gate(FakeLogin(LoginState.LOGGED_IN), FakeAccount()).assess("openai", TASK)
        self.assertEqual(
            result.evidence["account"], {"fingerprint": f"fp-{len(ACCOUNT_ID)}"}, msg="fingerprint"
        )
        self.assertNotIn(ACCOUNT_ID, repr(result.evidence), msg="the raw id is never shown")

    def test_an_unknown_identity_shows_no_account_entry(self) -> None:
        """No identity, no entry."""
        result = make_gate(FakeLogin(LoginState.LOGGED_IN), FakeAccount(identity=None)).assess(
            "openai", TASK
        )
        self.assertNotIn("account", result.evidence, msg="no entry")


if __name__ == "__main__":
    unittest.main()
