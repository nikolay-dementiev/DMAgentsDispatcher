"""Tests of the Codex account dialogue: its command and the reading of what the server answers."""

import json
import unittest
from pathlib import Path

from agents_dispatcher.adapters.system.codex_account import (
    HOLD_SECONDS,
    MAX_NOTIFICATIONS,
    CodexAccountDialogue,
)
from agents_dispatcher.domain.login import LoginState
from agents_dispatcher.domain.quota import EvidenceKind

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "codex"
ACCOUNT_ID = "00000000-0000-4000-8000-000000000004"


def server_output(name: str) -> str:
    """Return the lines the server sent in a captured dialogue, one JSON text per line."""
    lines = []
    for raw in (FIXTURES / name).read_text(encoding="utf-8").splitlines():
        entry = json.loads(raw)
        if "got" in entry:
            lines.append(json.dumps(entry["got"]))
    return "\n".join(lines) + "\n"


def with_rate_limits(output: str, *, account_id: object, used: float) -> str:
    """Return `output` with every rate-limit reply naming `account_id` and a primary `used`."""
    out = []
    for line in output.splitlines():
        value = json.loads(line)
        result = value.get("result", {})
        if "rateLimits" in result:
            result["accountId"] = account_id
            result["rateLimitsByLimitId"]["codex"]["primary"]["usedPercent"] = used
        out.append(json.dumps(value))
    return "\n".join(out) + "\n"


class CommandTests(unittest.TestCase):
    """The dialogue is one pipeline: the requests, a hold, then the server."""

    dialogue = CodexAccountDialogue("/opt/tools/codex")

    def test_the_server_runs_over_standard_streams_and_the_tool_is_an_argument(self) -> None:
        """The tool path is an argument of the shell, never part of the script text."""
        argv = self.dialogue.argv
        self.assertEqual(argv[:2], ("/bin/sh", "-c"), msg="a shell pipeline")
        self.assertIn("/opt/tools/codex", argv[4:], msg="the tool is a separate argument")
        self.assertNotIn("/opt/tools/codex", argv[2], msg="the script does not embed the path")
        self.assertIn("app-server", argv[2], msg="the app-server")
        self.assertIn("stdio://", argv[2], msg="over standard streams")

    def test_stdin_stays_open_until_the_last_reply_or_the_bound(self) -> None:
        """Closing at once ends the server after its first reply (measured offline).

        The writer waits for the last reply in the output file, at most `HOLD_SECONDS`.
        """
        script = self.dialogue.argv[2]
        self.assertIn("grep", script, msg="waits for the last reply")
        self.assertIn('"id":6', script, msg="the last request of the dialogue")
        self.assertIn(f"-lt {HOLD_SECONDS * 10}", script, msg="bounded by the hold")

    def test_the_hold_is_a_parameter(self) -> None:
        """A shorter hold bounds the wait of a test or a tighter inspector limit."""
        short = CodexAccountDialogue("/opt/tools/codex", hold_s=2)
        self.assertIn("-lt 20", short.argv[2], msg="two seconds, ten polls a second")

    def test_the_requests_are_the_measured_dialogue(self) -> None:
        """Initialize, initialized, then account and rate-limit reads."""
        script = "\n".join(self.dialogue.argv)
        for method in ("initialize", "initialized", "account/read", "account/rateLimits/read"):
            self.assertIn(method, script, msg=f"{method} is requested")


class AuthenticatedTests(unittest.TestCase):
    """The captured dialogue of a logged-in account."""

    reading = CodexAccountDialogue("/x/codex").interpret(
        0, server_output("appserver_dialogue.jsonl")
    )

    def test_login_and_identity_are_read(self) -> None:
        """An account object is a login; `rateLimits.accountId` is the identity."""
        self.assertEqual(self.reading.login, LoginState.LOGGED_IN, msg="logged in")
        self.assertEqual(self.reading.identity, ACCOUNT_ID, msg="the stable account id")

    def test_both_windows_of_the_bound_bucket_are_read(self) -> None:
        """The `codex` bucket carries the 5-hour and the weekly window."""
        quota = self.reading.quota
        self.assertEqual(quota.kind, EvidenceKind.SNAPSHOT, msg="a snapshot")
        self.assertEqual(
            [(w.minutes, w.used_percent) for w in quota.windows],
            [(300, 2), (10080, 36)],
            msg="primary and secondary",
        )
        self.assertEqual(quota.age_s, 0.0, msg="read now")


class UnauthenticatedTests(unittest.TestCase):
    """The captured dialogue without a login."""

    reading = CodexAccountDialogue("/x/codex").interpret(
        0, server_output("appserver_unauthenticated.jsonl")
    )

    def test_a_null_account_is_logged_out_with_no_identity(self) -> None:
        """No account, no identity."""
        self.assertEqual(self.reading.login, LoginState.LOGGED_OUT, msg="logged out")
        self.assertIsNone(self.reading.identity, msg="no identity")

    def test_the_rate_limit_error_is_unavailable_evidence(self) -> None:
        """The refusal of the read is not a quota reading."""
        self.assertEqual(self.reading.quota.kind, EvidenceKind.UNAVAILABLE, msg="unavailable")
        self.assertIn("authentication", self.reading.quota.detail, msg="the reason is kept")


class DamagedDialogueTests(unittest.TestCase):
    """Anything off the measured shape is unusable, never guessed."""

    dialogue = CodexAccountDialogue("/x/codex")
    good = server_output("appserver_dialogue.jsonl")

    def test_conflicting_account_ids_are_a_conflict_and_no_identity(self) -> None:
        """Two reads that name different accounts discard the exchange."""
        first = True
        out = []
        for line in self.good.splitlines():
            value = json.loads(line)
            if "rateLimits" in value.get("result", {}) and first:
                value["result"]["accountId"] = "other-account"
                first = False
            out.append(json.dumps(value))
        reading = self.dialogue.interpret(0, "\n".join(out) + "\n")
        self.assertEqual(reading.quota.kind, EvidenceKind.CONFLICT, msg="conflict")
        self.assertIsNone(reading.identity, msg="identity discarded")

    def test_a_missing_account_id_is_an_unknown_identity_not_a_failure(self) -> None:
        """Quota is still read; the identity is simply not known."""
        reading = self.dialogue.interpret(0, with_rate_limits(self.good, account_id=None, used=2))
        self.assertIsNone(reading.identity, msg="no identity")
        self.assertEqual(reading.quota.kind, EvidenceKind.SNAPSHOT, msg="quota stays usable")

    def test_an_empty_account_id_is_an_unknown_identity(self) -> None:
        """An empty id is not an id."""
        reading = self.dialogue.interpret(0, with_rate_limits(self.good, account_id="", used=2))
        self.assertIsNone(reading.identity, msg="no identity")

    def test_non_json_output_is_unusable(self) -> None:
        """A line that is not JSON makes the whole exchange unusable."""
        reading = self.dialogue.interpret(0, "not json\n" + self.good)
        self.assertEqual(reading.login, LoginState.UNKNOWN, msg="login unknown")
        self.assertEqual(reading.quota.kind, EvidenceKind.UNAVAILABLE, msg="quota unavailable")

    def test_hostile_json_is_unusable_not_a_crash(self) -> None:
        """An integer of thousands of digits or a deep nesting must not raise."""
        for line in ('{"id": ' + "9" * 5000 + "}", "[" * 100_000 + "]" * 100_000):
            with self.subTest(size=len(line)):
                reading = self.dialogue.interpret(0, line + "\n" + self.good)
                self.assertEqual(reading.login, LoginState.UNKNOWN, msg="login unknown")
                self.assertEqual(reading.quota.kind, EvidenceKind.UNAVAILABLE, msg="unavailable")

    def test_an_oversized_output_is_unusable(self) -> None:
        """The reader takes a bounded amount, however much the server printed."""
        reading = self.dialogue.interpret(0, "x" * 5_000_000)
        self.assertEqual(reading.login, LoginState.UNKNOWN, msg="login unknown")
        self.assertEqual(reading.quota.kind, EvidenceKind.UNAVAILABLE, msg="unavailable")
        self.assertIn("too large", reading.quota.detail, msg="the cause is named")

    def test_a_truncated_dialogue_is_unusable(self) -> None:
        """Replies that never came are not a state."""
        first = self.good.splitlines()[0] + "\n"
        reading = self.dialogue.interpret(0, first)
        self.assertEqual(reading.login, LoginState.UNKNOWN, msg="login unknown")
        self.assertEqual(reading.quota.kind, EvidenceKind.UNAVAILABLE, msg="quota unavailable")
        self.assertIsNone(reading.identity, msg="no identity")

    def test_a_failed_exit_is_unusable(self) -> None:
        """The server that exits with an error produced nothing to trust."""
        reading = self.dialogue.interpret(1, self.good)
        self.assertEqual(reading.login, LoginState.UNKNOWN, msg="login unknown")
        self.assertEqual(reading.quota.kind, EvidenceKind.UNAVAILABLE, msg="quota unavailable")

    def test_notifications_are_tolerated_up_to_a_bound(self) -> None:
        """Unsolicited messages pass; a flood does not."""
        noise = json.dumps({"method": "account/updated", "params": {}}) + "\n"
        already = sum(1 for line in self.good.splitlines() if "id" not in json.loads(line))
        ok = self.dialogue.interpret(0, noise * (MAX_NOTIFICATIONS - already) + self.good)
        self.assertEqual(ok.login, LoginState.LOGGED_IN, msg="within the bound")
        flood = self.dialogue.interpret(0, noise * (MAX_NOTIFICATIONS - already + 1) + self.good)
        self.assertEqual(flood.login, LoginState.UNKNOWN, msg="over the bound")

    def test_out_of_range_usage_is_not_a_snapshot(self) -> None:
        """The quota rules decide on sound windows only."""
        reading = self.dialogue.interpret(
            0, with_rate_limits(self.good, account_id=ACCOUNT_ID, used=250)
        )
        self.assertEqual(reading.quota.kind, EvidenceKind.UNAVAILABLE, msg="unusable windows")

    def test_account_reads_that_disagree_are_an_unknown_login(self) -> None:
        """A logged-in and a logged-out read in one dialogue are not a login state."""
        out = []
        second = 4  # the id of the second account read
        for line in self.good.splitlines():
            value = json.loads(line)
            if value.get("id") == second:
                value["result"]["account"] = None
            out.append(json.dumps(value))
        reading = self.dialogue.interpret(0, "\n".join(out) + "\n")
        self.assertEqual(reading.login, LoginState.UNKNOWN, msg="disagreement")


if __name__ == "__main__":
    unittest.main()
