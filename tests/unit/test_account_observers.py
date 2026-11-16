"""Tests of the observers that bind the confined inspector to one tool's probe or dialogue."""

import json
import unittest
from pathlib import Path

from agents_dispatcher.adapters.system.account_observers import (
    BoundLoginObserver,
    ConfinedAccountReader,
)
from agents_dispatcher.adapters.system.codex_account import CodexAccountDialogue
from agents_dispatcher.adapters.system.egress_proxy import ProxyDecision
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


class SpyLog:
    """Keeps every look the observers log."""

    def __init__(self) -> None:
        """Start empty."""
        self.looks: list[tuple[str, str, str, tuple[str, ...]]] = []

    def record(
        self, provider: str, look: str, outcome: str, destinations: tuple[str, ...] = ()
    ) -> None:
        """Keep one look."""
        self.looks.append((provider, look, outcome, destinations))


class FakeProxy:
    """A stand-in proxy; the reader passes it on and reads its decisions."""

    def decisions(self) -> tuple[ProxyDecision, ...]:
        """Report one allowed and one refused destination."""
        return (
            ProxyDecision(allowed=True, host="chatgpt.com", port=443, reason="allowlisted"),
            ProxyDecision(
                allowed=False,
                host="ab.chatgpt.com",
                port=443,
                reason="destination is not on the allowlist",
            ),
        )

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
        observation = BoundLoginObserver(inspector, FakeProbe(), "openai", SpyLog()).observe()
        self.assertEqual(observation.state, LoginState.LOGGED_IN, msg="the inspector's answer")
        self.assertEqual(inspector.argv_seen, [FakeProbe.argv], msg="the probe's command")

    def test_every_look_is_logged_with_its_outcome(self) -> None:
        """The provider, the kind of look and the state; no destinations without a proxy."""
        log = SpyLog()
        BoundLoginObserver(
            FakeInspector(NotCaptured("unused")), FakeProbe(), "openai", log
        ).observe()
        self.assertEqual(
            log.looks, [("openai", "login", "LOGGED_IN: via probe", ())], msg="one login look"
        )


class ConfinedAccountReaderTests(unittest.TestCase):
    """The reader runs the dialogue through the inspector, behind the proxy."""

    dialogue = CodexAccountDialogue("/opt/tools/codex")

    def test_a_captured_dialogue_is_read(self) -> None:
        """Output goes to the dialogue's reading; the dialogue command and the proxy are used."""
        inspector = FakeInspector(Captured(0, captured_dialogue()))
        proxy = FakeProxy()
        reading = ConfinedAccountReader(
            inspector, self.dialogue, lambda: proxy, "openai", SpyLog()
        ).read()
        self.assertEqual(reading.login, LoginState.LOGGED_IN, msg="login from the dialogue")
        self.assertEqual(reading.quota.kind, EvidenceKind.SNAPSHOT, msg="quota from the dialogue")
        self.assertEqual(inspector.argv_seen, [self.dialogue.argv], msg="the dialogue command")
        self.assertEqual(inspector.proxy_seen, [proxy], msg="behind the proxy")

    def test_nothing_captured_is_an_unusable_reading_with_the_reason(self) -> None:
        """A confinement trouble is unavailable evidence, never a guess."""
        inspector = FakeInspector(NotCaptured("sandbox-exec is not usable"))
        reading = ConfinedAccountReader(
            inspector, self.dialogue, FakeProxy, "openai", SpyLog()
        ).read()
        self.assertEqual(reading.login, LoginState.UNKNOWN, msg="login unknown")
        self.assertEqual(reading.quota.kind, EvidenceKind.UNAVAILABLE, msg="quota unavailable")
        self.assertIn("sandbox-exec", reading.quota.detail, msg="the reason is kept")
        self.assertIsNone(reading.identity, msg="no identity")

    def test_the_look_is_logged_with_the_proxy_decisions_and_never_the_account(self) -> None:
        """Hosts and verdicts, the outcome and the quota; no identity, no server text."""
        log = SpyLog()
        inspector = FakeInspector(Captured(0, captured_dialogue()))
        reading = ConfinedAccountReader(inspector, self.dialogue, FakeProxy, "openai", log).read()
        ((provider, look, outcome, destinations),) = log.looks
        self.assertTrue(reading.identity, msg="the dialogue names an account to leak")
        self.assertNotIn(str(reading.identity), outcome, msg="the account id is not in the outcome")
        self.assertNotIn(
            str(reading.identity), " ".join(destinations), msg="nor in the destinations"
        )
        self.assertEqual((provider, look), ("openai", "account"), msg="what was looked at")
        self.assertIn("login=LOGGED_IN", outcome, msg="the login")
        self.assertIn("quota=SNAPSHOT", outcome, msg="the quota kind")
        self.assertEqual(
            destinations,
            ("chatgpt.com:443=allow", "ab.chatgpt.com:443=refuse"),
            msg="host, port and verdict of each decision",
        )

    def test_a_look_that_captured_nothing_is_logged_with_the_cause(self) -> None:
        """The confinement trouble is in the log, not only in the reading."""
        log = SpyLog()
        inspector = FakeInspector(NotCaptured("sandbox-exec is not usable"))
        ConfinedAccountReader(inspector, self.dialogue, FakeProxy, "openai", log).read()
        ((_provider, _look, outcome, _destinations),) = log.looks
        self.assertIn("not captured", outcome, msg="the kind of failure")
        self.assertIn("sandbox-exec", outcome, msg="the cause")

    def test_every_look_gets_a_fresh_proxy_and_logs_only_its_own_decisions(self) -> None:
        """A proxy serves one look; a second look through the same reader must not reuse it."""
        made: list[FakeProxy] = []

        def factory() -> FakeProxy:
            proxy = FakeProxy()
            made.append(proxy)
            return proxy

        log = SpyLog()
        inspector = FakeInspector(Captured(0, captured_dialogue()))
        reader = ConfinedAccountReader(inspector, self.dialogue, factory, "openai", log)
        reader.read()
        reader.read()
        self.assertEqual(len(made), 2, msg="one proxy per look")
        self.assertEqual(inspector.proxy_seen, made, msg="each look used its own proxy")
        self.assertEqual(log.looks[0][3], log.looks[1][3], msg="the second look does not add up")


if __name__ == "__main__":
    unittest.main()
