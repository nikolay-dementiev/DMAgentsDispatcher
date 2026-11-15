"""Observers that bind the confined inspector to one tool: its login probe, its account dialogue."""

from typing import Protocol

from agents_dispatcher.adapters.system.codex_account import CodexAccountDialogue
from agents_dispatcher.adapters.system.login_inspector import (
    Captured,
    LoginProbe,
    NotCaptured,
)
from agents_dispatcher.application.ports import EgressProxy
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


class BoundLoginObserver:
    """Looks at the login of one tool through its probe."""

    def __init__(self, inspector: Inspector, probe: LoginProbe) -> None:
        """Keep the inspector and the probe."""
        self._inspector = inspector
        self._probe = probe

    def observe(self) -> LoginObservation:
        """Return the state the tool shows."""
        return self._inspector.observe(self._probe)


class ConfinedAccountReader:
    """Reads a tool's identity and quota through its account dialogue, behind its proxy."""

    def __init__(
        self, inspector: Inspector, dialogue: CodexAccountDialogue, proxy: EgressProxy
    ) -> None:
        """Keep the inspector, the dialogue and the proxy."""
        self._inspector = inspector
        self._dialogue = dialogue
        self._proxy = proxy

    def read(self) -> AccountReading:
        """Run the dialogue; a run that cannot be trusted is an unavailable reading."""
        captured = self._inspector.capture(self._dialogue.argv, self._proxy)
        if isinstance(captured, NotCaptured):
            return AccountReading(
                LoginState.UNKNOWN,
                None,
                QuotaEvidence(EvidenceKind.UNAVAILABLE, detail=captured.note),
            )
        return self._dialogue.interpret(captured.exit_status, captured.stdout)
