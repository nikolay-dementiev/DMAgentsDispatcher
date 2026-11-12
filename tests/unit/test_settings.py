"""Tests of the settings subset: shipped defaults, user override file and validation."""

import unittest

from agents_dispatcher.adapters.system.settings_loader import load_settings, shipped_defaults_text
from agents_dispatcher.application.settings import Settings, SettingsError

HOME = "/Users/example"


def load_shipped(user_text: str | None = None) -> Settings:
    """Load the shipped defaults, optionally under a user file, for a synthetic home."""
    return load_settings(
        defaults_text=shipped_defaults_text(), user_text=user_text, account_home=HOME
    )


class ShippedDefaultsTests(unittest.TestCase):
    """The defaults that ship with the package (ARCH section 3.2)."""

    def test_defaults_match_the_catalogue(self) -> None:
        """Every M1 key carries its documented default."""
        settings = load_shipped()
        self.assertEqual(settings.memory_bytes, 2_147_483_648, msg="resources.run.memory_bytes")
        self.assertEqual(settings.hard_s, 14400.0, msg="watchdog.hard_s")
        self.assertEqual(settings.term_grace_s, 10.0, msg="watchdog.term_grace_s")
        self.assertEqual(settings.kill_wait_s, 2.0, msg="watchdog.kill_wait_s")
        self.assertEqual(settings.anthropic_hosts, ("api.anthropic.com",), msg="proxy hosts")
        self.assertEqual(settings.proxy_ports, (443,), msg="proxy.ports")
        self.assertEqual(
            settings.read_deny,
            ("@sensitive-user-data", "@other-vendor-state", "@sdk-control", "@other-runs"),
            msg="sandbox.read_deny",
        )
        self.assertEqual(settings.worker_executable, "auto", msg="profiles._template.executable")

    def test_default_locations_resolve_under_the_account_home_and_never_under_tmp(self) -> None:
        """Durable roots live in account state, not in a temporary directory."""
        settings = load_shipped()
        self.assertEqual(
            settings.store_path, f"{HOME}/.local/state/agents-dispatcher", msg="store.path"
        )
        self.assertEqual(
            settings.run_area_path, f"{HOME}/.local/state/agents-dispatcher/runs", msg="run_area"
        )
        for path in (settings.store_path, settings.run_area_path):
            temporary_roots = ("/tmp", "/private/tmp", "/var/tmp")  # noqa: S108 - asserted absent
            self.assertFalse(path.startswith(temporary_roots), msg=path)

    def test_sensitive_inventories_resolve_against_the_account_home(self) -> None:
        """The shipped inventories hold absolute paths, so no list is hard-coded elsewhere."""
        settings = load_shipped()
        entries = settings.sensitive_user_data + settings.other_vendor_state
        self.assertIn(f"{HOME}/.ssh", settings.sensitive_user_data, msg="ssh keys are listed")
        self.assertTrue(all(p.startswith("/") for p in entries), msg="every entry is absolute")
        self.assertNotIn(
            f"{HOME}/Library/Keychains", settings.sensitive_user_data, msg="keychains stay readable"
        )


class OverrideTests(unittest.TestCase):
    """The user file and a changed default."""

    def test_a_user_value_replaces_the_default(self) -> None:
        """Only the named key changes."""
        settings = load_shipped('[watchdog]\nhard_s = "60"\n')
        self.assertEqual(settings.hard_s, 60.0, msg="override applied")
        self.assertEqual(settings.term_grace_s, 10.0, msg="neighbour untouched")

    def test_a_changed_default_takes_effect_without_a_code_change(self) -> None:
        """The defaults text is the single source of every value."""
        changed = shipped_defaults_text().replace('hard_s = "14400"', 'hard_s = "99"')
        settings = load_settings(defaults_text=changed, user_text=None, account_home=HOME)
        self.assertEqual(settings.hard_s, 99.0, msg="default read from the text")

    def test_a_user_path_token_is_resolved(self) -> None:
        """Tokens in a user value resolve like the shipped ones."""
        settings = load_shipped('[run_area]\npath = "@account-state/elsewhere"\n')
        self.assertEqual(settings.run_area_path, f"{HOME}/.local/state/elsewhere", msg="token")


class ValidationTests(unittest.TestCase):
    """Bad input is refused with a message that names the key."""

    def refused(self, user_text: str) -> str:
        """Return the message of the SettingsError the text provokes."""
        with self.assertRaises(SettingsError) as caught:
            load_shipped(user_text)
        return str(caught.exception)

    def test_an_unknown_key_is_refused(self) -> None:
        """A typo must not be silently ignored."""
        self.assertIn("watchdog.hard_sec", self.refused('[watchdog]\nhard_sec = "5"\n'), msg="key")

    def test_a_wrong_type_names_the_key(self) -> None:
        """A number where a string is required."""
        self.assertIn("watchdog.hard_s", self.refused("[watchdog]\nhard_s = 5\n"), msg="key")

    def test_a_non_positive_limit_is_refused(self) -> None:
        """Zero would stop every run at once."""
        self.assertIn(
            "resources.run.memory_bytes",
            self.refused("[resources.run]\nmemory_bytes = 0\n"),
            msg="key",
        )

    def test_a_port_out_of_range_is_refused(self) -> None:
        """Ports are 1 to 65535."""
        self.assertIn("proxy.ports", self.refused("[proxy]\nports = [0]\n"), msg="key")

    def test_an_unknown_selector_is_refused(self) -> None:
        """Only the four documented selectors exist."""
        self.assertIn(
            "sandbox.read_deny", self.refused('[sandbox]\nread_deny = ["@nothing"]\n'), msg="key"
        )

    def test_a_relative_path_is_refused(self) -> None:
        """Locations are absolute or tokens."""
        self.assertIn("store.path", self.refused('[store]\npath = "state"\n'), msg="key")

    def test_malformed_toml_is_refused_with_a_settings_error(self) -> None:
        """No bare traceback."""
        self.assertIn("settings", self.refused("[watchdog\n").lower(), msg="mentions settings")

    def test_a_wrong_schema_version_is_refused(self) -> None:
        """The schema version is an exact constant."""
        self.assertIn("schema_version", self.refused("schema_version = 2\n"), msg="key")

    def test_too_many_fraction_digits_are_refused(self) -> None:
        """Quoted decimal seconds carry at most nine fractional digits."""
        self.assertIn(
            "watchdog.kill_wait_s",
            self.refused('[watchdog]\nkill_wait_s = "1.0123456789"\n'),
            msg="key",
        )


if __name__ == "__main__":
    unittest.main()
