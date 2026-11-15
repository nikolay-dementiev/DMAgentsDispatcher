"""Tests of the account fingerprint: stable, short, and not the id."""

import re
import unittest

from agents_dispatcher.adapters.system.account_fingerprint import fingerprint


class FingerprintTests(unittest.TestCase):
    """An account id is shown only as its fingerprint."""

    def test_it_is_twelve_lowercase_hex_characters(self) -> None:
        """A short, fixed shape."""
        self.assertRegex(fingerprint("acct-0001"), re.compile(r"[0-9a-f]{12}"), msg="shape")

    def test_it_is_stable_and_tells_accounts_apart(self) -> None:
        """Same id, same mark; another id, another mark."""
        self.assertEqual(fingerprint("acct-0001"), fingerprint("acct-0001"), msg="stable")
        self.assertNotEqual(fingerprint("acct-0001"), fingerprint("acct-0002"), msg="distinct")

    def test_it_does_not_contain_the_id(self) -> None:
        """One-way: the id is not recoverable from the text."""
        self.assertNotIn("acct-0001", fingerprint("acct-0001"), msg="not the id")


if __name__ == "__main__":
    unittest.main()
