"""Login rules: what an observation of a tool's login allows (ARCH D7, PLAN-9 section 4).

This module is pure. An observation that could not be made is `UNKNOWN` and refuses nothing by
itself; a login that was seen to go from in to out during a run is recorded as lost and refuses
the provider until a fresh observation sees it logged in again.
"""

from enum import Enum

from agents_dispatcher.domain.routing import Problem, ProblemCode


class LoginState(Enum):
    """What the tool says about its own login."""

    LOGGED_IN = "LOGGED_IN"
    LOGGED_OUT = "LOGGED_OUT"
    UNKNOWN = "UNKNOWN"


def judge_login(state: LoginState, *, lost: bool) -> Problem | None:
    """Return the refusal for a fresh observation, or None; `lost` is an unresolved loss."""
    if state is LoginState.LOGGED_IN:
        return None
    if lost:
        text = "the login was lost during a run and no fresh observation has seen it logged in"
        return Problem(ProblemCode.LOGIN_LOST, text)
    if state is LoginState.LOGGED_OUT:
        return Problem(ProblemCode.AUTH_REQUIRED, "the tool is not logged in")
    return None


def login_was_lost(before: LoginState, after: LoginState) -> bool:
    """Return whether a run that began logged in ended logged out."""
    return before is LoginState.LOGGED_IN and after is LoginState.LOGGED_OUT


def clears_loss(state: LoginState) -> bool:
    """Return whether a fresh observation clears a recorded loss: only a seen login does."""
    return state is LoginState.LOGGED_IN
