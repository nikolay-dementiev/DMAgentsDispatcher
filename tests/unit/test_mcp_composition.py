"""Tests of the refusals of `agentsd mcp serve` before it takes over standard input and output."""

import io
import unittest
from unittest import mock

from agents_dispatcher.adapters.system.package_version import PackageMetadataVersionSource
from agents_dispatcher.adapters.system.settings_loader import load_settings, shipped_defaults_text
from agents_dispatcher.adapters.system.stdio_claim import ClaimedStdio
from agents_dispatcher.application.services import Services
from agents_dispatcher.composition import mcp as composition
from tests.support.cli_book import Book


def settings_with(user_text: str | None) -> object:
    """Load the shipped defaults under a user file for a synthetic home."""
    return load_settings(
        defaults_text=shipped_defaults_text(), user_text=user_text, account_home="/home/example"
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


class AbnormalEndTests(unittest.TestCase):
    """A non-zero exit always says why on standard error."""

    def test_a_failed_server_run_names_the_log_on_standard_error(self) -> None:
        """Exit code 1 with an actionable line, not a silent status."""
        stderr = io.StringIO()
        shipped = settings_with(None)
        stdio = ClaimedStdio(in_fd=-1, out_fd=-1)
        with (
            mock.patch.object(composition, "load_account_settings", return_value=shipped),
            mock.patch.object(PackageMetadataVersionSource, "installed_version", return_value="1"),
            mock.patch.object(composition, "claim_stdio", return_value=stdio),
            mock.patch.object(composition, "serve", return_value=1),
        ):
            code = composition.serve_account(Spy(), stderr)
        self.assertEqual(code, 1, msg="the server's code is passed on")
        self.assertIn("sdk.log", stderr.getvalue(), msg="points at the log")

    def test_a_clean_run_writes_nothing_to_standard_error(self) -> None:
        """Exit code 0 stays quiet."""
        stderr = io.StringIO()
        shipped = settings_with(None)
        stdio = ClaimedStdio(in_fd=-1, out_fd=-1)
        with (
            mock.patch.object(composition, "load_account_settings", return_value=shipped),
            mock.patch.object(PackageMetadataVersionSource, "installed_version", return_value="1"),
            mock.patch.object(composition, "claim_stdio", return_value=stdio),
            mock.patch.object(composition, "serve", return_value=0),
        ):
            code = composition.serve_account(Spy(), stderr)
        self.assertEqual((code, stderr.getvalue()), (0, ""), msg="quiet")


if __name__ == "__main__":
    unittest.main()
