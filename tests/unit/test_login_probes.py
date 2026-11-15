"""Tests of the login probes: the command of each tool and the reading of what it prints."""

import json
import unittest
from pathlib import Path

from agents_dispatcher.adapters.system.login_probes import ClaudeLoginProbe, CodexLoginProbe
from agents_dispatcher.domain.login import LoginState

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def text(name: str) -> str:
    """Return one scrubbed capture."""
    return (FIXTURES / name).read_text(encoding="utf-8")


class CodexProbeTests(unittest.TestCase):
    """`codex login status`: exit 0 with "Logged in ...", or exit 1 with "Not logged in"."""

    probe = CodexLoginProbe("/opt/tools/codex")

    def test_the_command_is_the_offline_status_command_with_stderr_joined_to_stdout(self) -> None:
        """Codex prints its status line on standard error; a shell wrapper joins the streams.

        The wrapper takes the tool and its arguments as separate arguments, never as a string.
        """
        self.assertEqual(
            self.probe.argv,
            ("/bin/sh", "-c", 'exec "$@" 2>&1', "sh", "/opt/tools/codex", "login", "status"),
            msg="login status only, streams joined",
        )

    def test_real_captures_are_read(self) -> None:
        """Logged in and logged out, as the CLI prints them."""
        self.assertEqual(
            self.probe.interpret(0, text("codex/login_status_in.txt")),
            LoginState.LOGGED_IN,
            msg="logged in",
        )
        self.assertEqual(
            self.probe.interpret(1, text("codex/login_status_out.txt")),
            LoginState.LOGGED_OUT,
            msg="logged out",
        )

    def test_a_warning_line_before_the_status_does_not_hide_it(self) -> None:
        """Real capture under the confinement: the missing PATH alias warning comes first."""
        self.assertEqual(
            self.probe.interpret(0, text("codex/login_status_in_confined.txt")),
            LoginState.LOGGED_IN,
            msg="logged in despite the warning",
        )

    def test_anything_else_is_unknown(self) -> None:
        """Contradictions, other statuses and missing exit status are not guessed at."""
        cases = (
            (0, "Not logged in\n"),
            (1, "Logged in using ChatGPT\n"),
            (3, "Logged in using ChatGPT\n"),
            (0, ""),
            (1, "something new\n"),
            (None, "Logged in using ChatGPT\n"),
        )
        for exit_status, output in cases:
            with self.subTest(exit_status=exit_status, output=output):
                self.assertEqual(
                    self.probe.interpret(exit_status, output), LoginState.UNKNOWN, msg="unknown"
                )


class HostileOutputTests(unittest.TestCase):
    """Output a vendor tool should never print still ends as UNKNOWN."""

    def test_hostile_json_is_unknown_not_a_crash(self) -> None:
        """Thousands of digits and deep nesting raise inside the JSON reader."""
        probe = ClaudeLoginProbe("/opt/tools/claude")
        for text in ('{"loggedIn": ' + "9" * 5000 + "}", "[" * 100_000):
            self.assertEqual(probe.interpret(0, text), LoginState.UNKNOWN, msg=f"{len(text)}")


class ClaudeProbeTests(unittest.TestCase):
    """`claude auth status`: the `loggedIn` boolean, nothing else."""

    probe = ClaudeLoginProbe("/opt/tools/claude")

    def test_the_command_is_the_offline_status_command(self) -> None:
        """No flags."""
        self.assertEqual(
            self.probe.argv, ("/opt/tools/claude", "auth", "status"), msg="auth status only"
        )

    def test_real_captures_are_read(self) -> None:
        """Logged in (with account fields that are ignored) and logged out."""
        self.assertEqual(
            self.probe.interpret(0, text("claude/auth_status_in.json")),
            LoginState.LOGGED_IN,
            msg="logged in",
        )
        self.assertEqual(
            self.probe.interpret(1, text("claude/auth_status_out.json")),
            LoginState.LOGGED_OUT,
            msg="logged out",
        )

    def test_anything_else_is_unknown(self) -> None:
        """Not JSON, no boolean, a string, an array, or a contradicting exit status."""
        cases = (
            (0, "not json"),
            (0, "{}"),
            (0, json.dumps({"loggedIn": "yes"})),
            (0, json.dumps([True])),
            (0, json.dumps({"loggedIn": False})),
            (1, json.dumps({"loggedIn": True})),
            (None, json.dumps({"loggedIn": True})),
        )
        for exit_status, output in cases:
            with self.subTest(exit_status=exit_status, output=output):
                self.assertEqual(
                    self.probe.interpret(exit_status, output), LoginState.UNKNOWN, msg="unknown"
                )


if __name__ == "__main__":
    unittest.main()
