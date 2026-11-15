"""The look at a provider's account around one run: before the launch and after it (D7).

Before the launch the login is observed again and, when admission knew the account, the account
is compared with it: a known change refuses the launch. After the run the login is observed once
more; a login seen to go from in to out is recorded as lost, and a seen login clears a recorded
loss. An observation that cannot be made refuses nothing.
"""

from collections.abc import Callable, Mapping
from typing import Protocol

from agents_dispatcher.application.account_gates import AccountReader, LoginMarks, LoginObserver
from agents_dispatcher.domain.login import (
    LoginObservation,
    LoginState,
    clears_loss,
    judge_login,
    login_was_lost,
)
from agents_dispatcher.domain.routing import (
    Disposition,
    Problem,
    ProblemCode,
    RunRouting,
)
from agents_dispatcher.domain.run import RunId

PRE_LAUNCH = "pre_launch"
POST_RUN = "post_run"


class AccountLedger(LoginMarks, Protocol):
    """Login-loss marks plus the facts observed around each run."""

    def mark_lost(self, provider: str) -> None:
        """Record that the login of `provider` was lost during a run."""
        ...

    def record_observation(self, run_id: RunId, phase: str, facts: dict[str, object]) -> None:
        """Keep the facts observed in one phase of a run."""
        ...

    def observations(self, run_id: RunId) -> Mapping[str, Mapping[str, object]]:
        """Return the facts recorded for a run, by phase."""
        ...


class ProviderAccountWatch:
    """Watches one provider's account; `account` is None for a tool with no account interface."""

    def __init__(
        self,
        *,
        login: LoginObserver,
        account: AccountReader | None,
        ledger: AccountLedger,
        fingerprint: Callable[[str], str],
    ) -> None:
        """Keep the observers, the ledger and the way an account id is shown."""
        self._login = login
        self._account = account
        self._ledger = ledger
        self._fingerprint = fingerprint

    def before_launch(self, run_id: RunId, routing: RunRouting) -> Problem | None:
        """Observe the login (and the account, when admission knew it); refuse on a bad look."""
        provider = routing.provider
        observation = self._login.observe()
        state = observation.state
        self._ledger.record_observation(run_id, PRE_LAUNCH, _facts(observation))
        refusal = judge_login(state, lost=self._ledger.is_lost(provider))
        if refusal is not None:
            return refusal
        if clears_loss(state) and self._ledger.is_lost(provider):
            self._ledger.clear_lost(provider)
        return self._account_change(routing)

    def after_run(self, run_id: RunId, routing: RunRouting) -> tuple[str, ...]:
        """Observe the login again; record a loss, clear one, and return notes for the report."""
        provider = routing.provider
        observation = self._login.observe()
        state = observation.state
        before = self._ledger.observations(run_id).get(PRE_LAUNCH, {}).get("login")
        self._ledger.record_observation(run_id, POST_RUN, _facts(observation))
        notes: list[str] = []
        if state is LoginState.UNKNOWN:
            cause = f": {observation.note}" if observation.note else ""
            notes.append(f"the {provider} login could not be observed after the run{cause}")
        if before == LoginState.LOGGED_IN.value and login_was_lost(LoginState.LOGGED_IN, state):
            self._ledger.mark_lost(provider)
            notes.append(f"the {provider} login was lost during the run")
        elif clears_loss(state) and self._ledger.is_lost(provider):
            self._ledger.clear_lost(provider)
        return tuple(notes)

    def _account_change(self, routing: RunRouting) -> Problem | None:
        """Return ACCOUNT_CHANGED when admission and now are both known and differ."""
        admitted = _admitted_fingerprint(routing)
        if admitted is None or self._account is None:
            return None
        identity = self._account.read().identity
        if identity is None or self._fingerprint(identity) == admitted:
            return None
        return Problem(
            ProblemCode.ACCOUNT_CHANGED,
            f"the {routing.provider} account is not the one the run was admitted with",
        )


def _facts(observation: LoginObservation) -> dict[str, object]:
    """Return the facts kept for one look: the state, and why it was not clean."""
    facts: dict[str, object] = {"login": observation.state.value}
    if observation.note:
        facts["note"] = observation.note
    return facts


def _admitted_fingerprint(routing: RunRouting) -> str | None:
    """Return the account fingerprint the selected link showed at admission, if any."""
    for evaluation in routing.route:
        if (
            evaluation.provider == routing.provider
            and evaluation.disposition is Disposition.SELECTED
        ):
            account = evaluation.evidence.get("account")
            shown = account.get("fingerprint") if isinstance(account, dict) else None
            if isinstance(shown, str):
                return shown
    return None
