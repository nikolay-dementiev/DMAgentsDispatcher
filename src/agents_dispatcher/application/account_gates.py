"""The account gate of a provider link: its login first, then its quota (PLAN-9 sections 4 and 6).

The gate feeds the pure rules of the domain with what the account interfaces read and returns the
refusals, the warnings and the evidence a route shows. A login that cannot be observed refuses
nothing; a quota that cannot be read is judged by `quota_mode`. The raw account id is compared
elsewhere and never shown: the evidence carries its fingerprint.
"""

from collections.abc import Callable
from typing import Protocol

from agents_dispatcher.application.routing import GateResult
from agents_dispatcher.domain.account import AccountReading
from agents_dispatcher.domain.login import LoginObservation, clears_loss, judge_login
from agents_dispatcher.domain.quota import (
    EvidenceKind,
    QuotaEvidence,
    QuotaMode,
    QuotaVerdict,
    judge_quota,
)
from agents_dispatcher.domain.routing import Problem, ResolvedTask


class LoginObserver(Protocol):
    """Looks at the login of one tool, confined."""

    def observe(self) -> LoginObservation:
        """Return the state the tool shows, or `UNKNOWN` with a note."""
        ...


class AccountReader(Protocol):
    """Reads identity and quota from a tool's account interface, confined."""

    def read(self) -> AccountReading:
        """Return what the interface showed; an unusable exchange is an unavailable reading."""
        ...


class LoginMarks(Protocol):
    """The recorded login losses of the providers."""

    def is_lost(self, provider: str) -> bool:
        """Return whether a loss is recorded and not yet cleared."""
        ...

    def clear_lost(self, provider: str) -> None:
        """Forget the recorded loss."""
        ...


class ProviderAccountGate:
    """Assesses one provider link; `account` is None for a tool with no quota interface."""

    def __init__(  # noqa: PLR0913 - the composition root wires exactly these collaborators
        self,
        *,
        login: LoginObserver,
        account: AccountReader | None,
        marks: LoginMarks,
        mode: QuotaMode,
        reserve_percent: int,
        max_age_s: float,
        fingerprint: Callable[[str], str],
    ) -> None:
        """Keep the observers, the quota rules and the way an account id is shown."""
        self._login = login
        self._account = account
        self._marks = marks
        self._mode = mode
        self._reserve = reserve_percent
        self._max_age = max_age_s
        self._fingerprint = fingerprint

    def assess(self, provider: str, task: ResolvedTask) -> GateResult:  # noqa: ARG002
        """Judge the login; when it passes, judge the quota."""
        observation = self._login.observe()
        evidence: dict[str, object] = {"login": _login_facts(observation)}
        lost = self._marks.is_lost(provider)
        refusal = judge_login(observation.state, lost=lost)
        if refusal is not None:
            return GateResult((refusal,), evidence=evidence)
        if lost and clears_loss(observation.state):
            self._marks.clear_lost(provider)
        reading = self._reading()
        if reading is not None and reading.identity is not None:
            evidence["account"] = {"fingerprint": self._fingerprint(reading.identity)}
        verdict = self._verdict(reading)
        evidence["quota"] = verdict.facts
        problems = (verdict.refusal,) if verdict.refusal else ()
        warnings: tuple[Problem, ...] = (verdict.warning,) if verdict.warning else ()
        return GateResult(problems, warnings, evidence)

    def _reading(self) -> AccountReading | None:
        """Read the account when quota is judged and the tool has an interface."""
        if self._mode is QuotaMode.OFF or self._account is None:
            return None
        return self._account.read()

    def _verdict(self, reading: AccountReading | None) -> QuotaVerdict:
        """Apply the quota rules to what was read."""
        evidence: QuotaEvidence | None = reading.quota if reading else None
        if reading is None and self._account is None and self._mode is not QuotaMode.OFF:
            evidence = QuotaEvidence(EvidenceKind.UNSUPPORTED)
        return judge_quota(self._mode, self._reserve, self._max_age, evidence)


def _login_facts(observation: LoginObservation) -> dict[str, object]:
    """Return the login evidence: the state, and the note when the look was not clean."""
    facts: dict[str, object] = {"state": observation.state.value}
    if observation.note:
        facts["note"] = observation.note
    return facts
