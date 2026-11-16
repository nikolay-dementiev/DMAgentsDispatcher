"""Tests of the refusals of `agentsd mcp serve` before it takes over standard input and output."""

import io
import unittest
from unittest import mock

from agents_dispatcher.adapters.system.package_version import PackageMetadataVersionSource
from agents_dispatcher.adapters.system.settings_loader import load_settings, shipped_defaults_text
from agents_dispatcher.application.services import Services
from agents_dispatcher.composition import mcp as composition
from tests.support.cli_book import Book


def settings_with(user_text: str | None) -> object:
    """Load the shipped defaults under a user file for a synthetic home."""
    return load_settings(
        defaults_text=shipped_defaults_text(), user_text=user_text, account_home="/Users/example"
    )


class Spy:
    """Counts how often the use cases were built."""

    def __init__(self) -> None:
        """Start at zero."""
        self.built = 0

    def __call__(self) -> Services:
        """Build the fake use cases."""
        self.built += 1
        return Book().services()


class RefusalTests(unittest.TestCase):
    """The server refuses before it builds anything or claims a descriptor."""

    def test_the_sdk_transport_is_refused_naming_the_setting_and_the_extra(self) -> None:
        """Exit code 2; no use case is built, so no store file is created."""
        spy, stderr = Spy(), io.StringIO()
        sdk = settings_with('[mcp]\ntransport = "sdk"\n')
        with mock.patch.object(composition, "load_account_settings", return_value=sdk):
            code = composition.serve_account(spy, stderr)
        self.assertEqual(code, 2, msg="invalid configuration")
        self.assertIn("mcp.transport", stderr.getvalue(), msg="names the setting")
        self.assertIn("mcp-sdk", stderr.getvalue(), msg="names the extra")
        self.assertEqual(spy.built, 0, msg="nothing was built")

    def test_a_missing_package_version_is_refused(self) -> None:
        """The server cannot name its version; the owner is told to reinstall."""
        spy, stderr = Spy(), io.StringIO()
        shipped = settings_with(None)
        with (
            mock.patch.object(composition, "load_account_settings", return_value=shipped),
            mock.patch.object(PackageMetadataVersionSource, "installed_version", return_value=None),
            mock.patch.object(composition, "claim_stdio") as claim,
        ):
            code = composition.serve_account(spy, stderr)
        self.assertEqual(code, 1, msg="failure")
        self.assertIn("reinstall the package", stderr.getvalue(), msg="says what to do")
        self.assertEqual(spy.built, 0, msg="nothing was built")
        claim.assert_not_called()


if __name__ == "__main__":
    unittest.main()
