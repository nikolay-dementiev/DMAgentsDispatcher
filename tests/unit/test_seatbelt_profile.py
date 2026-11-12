"""Tests of the Seatbelt profile text (digest section 2). Real enforcement is tested in process tests."""

import unittest
from dataclasses import replace

from agents_dispatcher.adapters.system.seatbelt_profile import ProfileInputs, render_profile

BASE = ProfileInputs(
    sandbox_dir="/private/var/runs/agentsd-run-1",
    proxy_port=48123,
    denied_reads=("/Users/u/.ssh", "/Users/u/.aws", "/private/var/runs"),
)


class RenderProfileTests(unittest.TestCase):
    """The profile is generated from inputs, never from a fixed text."""

    def test_writes_are_denied_everywhere_except_the_run_sandbox_and_listed_devices(self) -> None:
        """One deny of all writes, then one allow for the sandbox and the device files."""
        text = render_profile(BASE)
        self.assertIn("(deny file-write*)", text, msg="writes denied by default")
        self.assertIn('(subpath "/private/var/runs/agentsd-run-1")', text, msg="sandbox writable")
        self.assertIn('(literal "/dev/null")', text, msg="a listed device file is writable")
        self.assertEqual(text.count("(allow file-write*"), 1, msg="a single write allowance")

    def test_every_denied_read_is_listed_and_the_sandbox_is_readable_again_after_them(self) -> None:
        """The later allow wins, so the run's own folder stays readable inside a denied area."""
        text = render_profile(BASE)
        for path in BASE.denied_reads:
            self.assertIn(f'(deny file-read* (subpath "{path}"))', text, msg=path)
        last_deny = text.rindex("(deny file-read*")
        allow_sandbox = text.index('(allow file-read* (subpath "/private/var/runs/agentsd-run-1"))')
        self.assertGreater(allow_sandbox, last_deny, msg="the sandbox allowance comes last")

    def test_the_keychains_are_not_denied(self) -> None:
        """Denying them breaks the worker's login."""
        self.assertNotIn("Keychains", render_profile(BASE), msg="no rule names the keychains")

    def test_the_network_is_closed_except_for_the_proxy_port_on_loopback(self) -> None:
        """No other port, no Unix socket, no DNS."""
        text = render_profile(BASE)
        self.assertIn("(deny network*)", text, msg="network closed")
        self.assertIn('(allow network-outbound (remote ip "localhost:48123"))', text, msg="proxy")
        self.assertEqual(text.count("(allow network"), 1, msg="exactly one network allowance")

    def test_signals_and_mach_lookups_follow_the_digest(self) -> None:
        """Signals only to self and the same sandbox; only the security service by mach name."""
        text = render_profile(BASE)
        for rule in (
            "(deny signal (target others))",
            "(allow signal (target self))",
            "(allow signal (target same-sandbox))",
            "(deny mach-lookup)",
            '(allow mach-lookup (global-name "com.apple.SecurityServer"))',
        ):
            self.assertIn(rule, text, msg=rule)

    def test_a_path_with_a_quote_or_backslash_is_escaped(self) -> None:
        """Nothing a path contains can end the string literal early."""
        nasty = replace(BASE, denied_reads=('/Users/u/a"b\\c',))
        self.assertIn('(subpath "/Users/u/a\\"b\\\\c")', render_profile(nasty), msg="escaped")

    def test_unsafe_inputs_are_refused(self) -> None:
        """Relative paths, control characters and bad ports never reach the profile."""
        cases = {
            "relative sandbox": replace(BASE, sandbox_dir="runs/x"),
            "newline in a path": replace(BASE, denied_reads=("/Users/u/a\n(allow default)",)),
            "relative denied path": replace(BASE, denied_reads=("x",)),
            "port zero": replace(BASE, proxy_port=0),
            "port too large": replace(BASE, proxy_port=70000),
        }
        for name, inputs in cases.items():
            with self.subTest(case=name), self.assertRaises(ValueError):
                render_profile(inputs)


if __name__ == "__main__":
    unittest.main()
