"""The README states the routing contract: a changed flag, exit code or limit shows here."""

import unittest
from pathlib import Path

README = Path(__file__).resolve().parents[2] / "README.md"


class ReadmeTests(unittest.TestCase):
    """Each fact a caller relies on is written down."""

    def setUp(self) -> None:
        """Read the README once per test."""
        self.text = README.read_text(encoding="utf-8")

    def test_the_selection_flags_and_the_default_mode_are_documented(self) -> None:
        """`--task-type`, `--provider`, and that a result branch needs `--task-type implement`."""
        for phrase in ("--task-type", "--provider", "--task-type implement", "read task"):
            self.assertIn(phrase, self.text, msg=phrase)

    def test_the_three_decisions_and_their_exit_codes_are_documented(self) -> None:
        """Started, recommendation (exit 0, no run), rejection (exit 20) and authority (24)."""
        for phrase in ("RECOMMENDATION", "REJECTED", "NO_PROVIDER_ELIGIBLE", "| 20 |", "| 24 |"):
            self.assertIn(phrase, self.text, msg=phrase)

    def test_the_limits_of_the_first_version_are_stated(self) -> None:
        """No rotation of the SDK log, no preserved scratch files, unsupported policy rows."""
        for phrase in ("sdk.log", "not rotated", "temporary area", "write_roots"):
            self.assertIn(phrase, self.text, msg=phrase)

    def test_the_codex_provider_and_the_account_refusals_are_documented(self) -> None:
        """The `openai` link runs Codex; login, quota and capacity refusals have their codes."""
        for phrase in (
            "Codex",
            "AUTH_REQUIRED",
            "LOGIN_LOST",
            "QUOTA_RESERVE",
            "QUOTA_STALE",
            "QUOTA_UNAVAILABLE",
            "QUOTA_UNSUPPORTED",
            "QUOTA_CONFLICT",
            "ACCOUNT_CHANGED",
            "CAPACITY_CONTENDED",
        ):
            self.assertIn(phrase, self.text, msg=phrase)

    def test_the_account_settings_are_documented(self) -> None:
        """Every key that steers the quota and capacity rules is named."""
        for phrase in (
            "defaults.quota_mode",
            "quota_reserve_percent",
            "quota_max_age_s",
            "providers.<id>.capacity",
            "bound_bucket",
            "inspector.timeout_s",
        ):
            self.assertIn(phrase, self.text, msg=phrase)

    def test_the_limits_of_the_account_checks_are_stated(self) -> None:
        """What the login and quota looks cannot promise is written down."""
        for phrase in ("auth.json", "no quota cache", "no quota interface", "apply_patch"):
            self.assertIn(phrase, self.text, msg=phrase)


if __name__ == "__main__":
    unittest.main()
