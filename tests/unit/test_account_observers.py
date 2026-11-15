"""Tests of the observers that bind the confined inspector to one tool's probe or dialogue."""

import json
import unittest
from pathlib import Path

from agents_dispatcher.adapters.system.account_observers import (
    BoundLoginObserver,
    ConfinedAccountReader,
)
from agents_dispatcher.adapters.system.codex_account import CodexAccountDialogue
from agents_dispatcher.adapters.system.login_inspector import Captured, LoginProbe, NotCaptured
from agents_dispatcher.application.ports import EgressEndpoint, EgressProxy
from agents_dispatcher.domain.login import LoginObservation, LoginState
from agents_dispatcher.domain.quota import EvidenceKind
from agents_dispatcher.domain.run import RunId

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "codex" / "appserver_dialogue.jsonl"


class FakeProbe:
    """A probe with a fixed argv."""

    argv = ("/opt/tools/tool", "status")

    @staticmethod
    def interpret(exit_status: int | None, stdout: str) -> LoginState:  # noqa: ARG004 - unused
        """Never used by the observer itself."""
        return LoginState.UNKNOWN


class FakeInspector:
    """An inspector that answers a canned capture and keeps what it was asked."""

    def __init__(self, answer: Captured | NotCaptured) -> None:
        """Keep the answer."""
        self.answer = answer
        self.argv_seen: list[tuple[str, ...]] = []
        self.proxy_seen: list[EgressProxy | None] = []

    def observe(self, probe: LoginProbe) -> LoginObservation:
        """Return a state, to show the probe was used."""
        self.argv_seen.append(probe.argv)
        return LoginObservation(LoginState.LOGGED_IN, "via probe")

    def capture(
        self, argv: tuple[str, ...], proxy: EgressProxy | None = None
    ) -> Captured | NotCaptured:
        """Keep the call and return the answer."""
        self.argv_seen.append(argv)
        self.proxy_seen.append(proxy)
        return self.answer


class FakeProxy:
    """A stand-in proxy; the reader only passes it on."""

    def start(self, run_id: RunId) -> EgressEndpoint:  # noqa: ARG002 - fixed address
        """Report a fixed address."""
        return EgressEndpoint(host="127.0.0.1", port=1)

    def stop(self) -> None:
        """Nothing to stop."""

    def faults(self) -> tuple[str, ...]:
        """Report none."""
        return ()


def captured_dialogue() -> str:
    """Return the server lines of the captured dialogue."""
    lines = []
    for raw in FIXTURE.read_text(encoding="utf-8").splitlines():
        entry = json.loads(raw)
        if "got" in entry:
            lines.append(json.dumps(entry["got"]))
    return "\n".join(lines) + "\n"


class BoundLoginObserverTests(unittest.TestCase):
    """The observer asks the inspector about its own probe."""

    def test_it_observes_with_its_probe(self) -> None:
        """The inspector's answer is returned for the bound probe."""
        inspector = FakeInspector(NotCaptured("unused"))
        observation = BoundLoginObserver(inspector, FakeProbe()).observe()
        self.assertEqual(observation.state, LoginState.LOGGED_IN, msg="the inspector's answer")
        self.assertEqual(inspector.argv_seen, [FakeProbe.argv], msg="the probe's command")


class ConfinedAccountReaderTests(unittest.TestCase):
    """The reader runs the dialogue through the inspector, behind the proxy."""

    dialogue = CodexAccountDialogue("/opt/tools/codex")

    def test_a_captured_dialogue_is_read(self) -> None:
        """Output goes to the dialogue's reading; the dialogue command and the proxy are used."""
        inspector = FakeInspector(Captured(0, captured_dialogue()))
        proxy = FakeProxy()
        reading = ConfinedAccountReader(inspector, self.dialogue, proxy).read()
        self.assertEqual(reading.login, LoginState.LOGGED_IN, msg="login from the dialogue")
        self.assertEqual(reading.quota.kind, EvidenceKind.SNAPSHOT, msg="quota from the dialogue")
        self.assertEqual(inspector.argv_seen, [self.dialogue.argv], msg="the dialogue command")
        self.assertEqual(inspector.proxy_seen, [proxy], msg="behind the proxy")

    def test_nothing_captured_is_an_unusable_reading_with_the_reason(self) -> None:
        """A confinement trouble is unavailable evidence, never a guess."""
        inspector = FakeInspector(NotCaptured("sandbox-exec is not usable"))
        reading = ConfinedAccountReader(inspector, self.dialogue, FakeProxy()).read()
        self.assertEqual(reading.login, LoginState.UNKNOWN, msg="login unknown")
        self.assertEqual(reading.quota.kind, EvidenceKind.UNAVAILABLE, msg="quota unavailable")
        self.assertIn("sandbox-exec", reading.quota.detail, msg="the reason is kept")
        self.assertIsNone(reading.identity, msg="no identity")


if __name__ == "__main__":
    unittest.main()
