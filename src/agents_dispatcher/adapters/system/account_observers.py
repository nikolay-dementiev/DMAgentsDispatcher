"""Observers that bind the confined inspector to one tool: its login probe, its account dialogue."""

from typing import Protocol

from agents_dispatcher.adapters.system.codex_account import CodexAccountDialogue
from agents_dispatcher.adapters.system.egress_proxy import ProxyDecision
from agents_dispatcher.adapters.system.login_inspector import (
    Captured,
    LoginProbe,
    NotCaptured,
)
from agents_dispatcher.application.ports import EgressProxy, InspectionLog
from agents_dispatcher.domain.account import AccountReading
from agents_dispatcher.domain.login import LoginObservation, LoginState
from agents_dispatcher.domain.quota import EvidenceKind, QuotaEvidence


class Inspector(Protocol):
    """What the observers need of the confined inspector."""

    def observe(self, probe: LoginProbe) -> LoginObservation:
        """Run a login probe confined."""
        ...

    def capture(
        self, argv: tuple[str, ...], proxy: EgressProxy | None = None
    ) -> Captured | NotCaptured:
        """Run a command confined and return what it printed."""
        ...


class DecidingProxy(EgressProxy, Protocol):
    """An egress proxy that can also tell what it allowed and refused."""

    def decisions(self) -> tuple[ProxyDecision, ...]:
        """Return every decision so far."""
        ...


class BoundLoginObserver:
    """Looks at the login of one tool through its probe, and logs the look."""

    def __init__(
        self, inspector: Inspector, probe: LoginProbe, provider: str, log: InspectionLog
    ) -> None:
        """Keep the inspector, the probe, the provider's name and the log."""
        self._inspector = inspector
        self._probe = probe
        self._provider = provider
        self._log = log

    def observe(self) -> LoginObservation:
        """Return the state the tool shows."""
        observation = self._inspector.observe(self._probe)
        note = f": {observation.note}" if observation.note else ""
        self._log.record(self._provider, "login", f"{observation.state.name}{note}")
        return observation


class ConfinedAccountReader:
    """Reads a tool's identity and quota through its account dialogue, behind its proxy."""

    def __init__(
        self,
        inspector: Inspector,
        dialogue: CodexAccountDialogue,
        proxy: DecidingProxy,
        provider: str,
        log: InspectionLog,
    ) -> None:
        """Keep the inspector, the dialogue, the proxy, the provider's name and the log."""
        self._inspector = inspector
        self._dialogue = dialogue
        self._proxy = proxy
        self._provider = provider
        self._log = log

    def read(self) -> AccountReading:
        """Run the dialogue; a run that cannot be trusted is an unavailable reading."""
        captured = self._inspector.capture(self._dialogue.argv, self._proxy)
        destinations = tuple(
            f"{item.host}:{item.port}={'allow' if item.allowed else 'refuse'}"
            for item in self._proxy.decisions()
        )
        if isinstance(captured, NotCaptured):
            self._log.record(
                self._provider, "account", f"not captured: {captured.note}", destinations
            )
            return AccountReading(
                LoginState.UNKNOWN,
                None,
                QuotaEvidence(EvidenceKind.UNAVAILABLE, detail=captured.note),
            )
        reading = self._dialogue.interpret(captured.exit_status, captured.stdout)
        # Never the identity: the kinds and the detail (ours, plus a server error message if any).
        outcome = f"login={reading.login.name} quota={reading.quota.kind.name}"
        if reading.quota.detail:
            outcome += f" ({reading.quota.detail})"
        self._log.record(self._provider, "account", outcome, destinations)
        return reading
