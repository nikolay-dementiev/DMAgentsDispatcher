"""Tests of the login rules: what a login observation allows, and when a login counts as lost."""

import unittest

from agents_dispatcher.domain.login import (
    LoginState,
    clears_loss,
    judge_login,
    login_was_lost,
)
from agents_dispatcher.domain.routing import ProblemCode


class JudgeLoginTests(unittest.TestCase):
    """AUTH_REQUIRED for a logged-out account, LOGIN_LOST while a loss is unresolved."""

    def test_logged_in_admits(self) -> None:
        """Nothing to refuse."""
        self.assertIsNone(judge_login(LoginState.LOGGED_IN, lost=False), msg="admitted")
        self.assertIsNone(judge_login(LoginState.LOGGED_IN, lost=True), msg="a fresh login admits")

    def test_logged_out_is_auth_required(self) -> None:
        """The tool says it is not logged in."""
        problem = judge_login(LoginState.LOGGED_OUT, lost=False)
        assert problem is not None  # noqa: S101 - narrows the type for the checker
        self.assertEqual(problem.code, ProblemCode.AUTH_REQUIRED, msg="refused")

    def test_unknown_refuses_nothing_when_no_loss_is_on_record(self) -> None:
        """An observation that could not be made is not evidence of a logout."""
        self.assertIsNone(judge_login(LoginState.UNKNOWN, lost=False), msg="admitted")

    def test_a_recorded_loss_refuses_until_a_fresh_login_is_seen(self) -> None:
        """LOGIN_LOST wins over AUTH_REQUIRED and also holds for an unknown state."""
        for state in (LoginState.LOGGED_OUT, LoginState.UNKNOWN):
            with self.subTest(state=state):
                problem = judge_login(state, lost=True)
                assert problem is not None  # noqa: S101 - narrows the type for the checker
                self.assertEqual(problem.code, ProblemCode.LOGIN_LOST, msg="refused as lost")


class LossTests(unittest.TestCase):
    """A login is lost when a run began logged in and ended logged out."""

    def test_only_logged_in_then_logged_out_is_a_loss(self) -> None:
        """Every other pair, including unknown on either side, is not."""
        pairs = {
            (LoginState.LOGGED_IN, LoginState.LOGGED_OUT): True,
            (LoginState.LOGGED_IN, LoginState.LOGGED_IN): False,
            (LoginState.LOGGED_IN, LoginState.UNKNOWN): False,
            (LoginState.UNKNOWN, LoginState.LOGGED_OUT): False,
            (LoginState.LOGGED_OUT, LoginState.LOGGED_OUT): False,
            (LoginState.LOGGED_OUT, LoginState.LOGGED_IN): False,
        }
        for (before, after), expected in pairs.items():
            with self.subTest(before=before, after=after):
                self.assertEqual(login_was_lost(before, after), expected, msg="loss rule")

    def test_only_a_fresh_logged_in_clears_a_loss(self) -> None:
        """Unknown does not clear it."""
        self.assertTrue(clears_loss(LoginState.LOGGED_IN), msg="logged in clears")
        self.assertFalse(clears_loss(LoginState.UNKNOWN), msg="unknown keeps")
        self.assertFalse(clears_loss(LoginState.LOGGED_OUT), msg="logged out keeps")


if __name__ == "__main__":
    unittest.main()
