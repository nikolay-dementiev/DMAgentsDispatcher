"""Tests of the Seatbelt profile text (digest section 2); enforcement is in process tests."""

import unittest
from dataclasses import replace

from agents_dispatcher.adapters.system.seatbelt_profile import (
    CLAUDE_CODE,
    CODEX_MACH_SERVICES,
    ProfileInputs,
    ProviderProfile,
    VendorWrite,
    codex_profile,
    render_profile,
)
from agents_dispatcher.domain.routing import TaskMode

BASE = ProfileInputs(
    sandbox_dir="/private/var/runs/agentsd-run-1",
    proxy_port=48123,
    denied_reads=("/Users/u/.ssh", "/Users/u/.aws", "/private/var/runs"),
    mode=TaskMode.WRITE,
    worktree_dir="/private/var/runs/agentsd-run-1/worktree",
    provider=CLAUDE_CODE,
)
READ = replace(BASE, mode=TaskMode.READ)
SCRATCH = "tmp"  # the worker's temporary folder inside the sandbox


class ReadProfileTests(unittest.TestCase):
    """A read task may write only its temporary areas; the worktree is closed explicitly."""

    def test_only_the_temporary_areas_and_devices_are_writable(self) -> None:
        """The sandbox as a whole is not granted, its two scratch folders are."""
        text = render_profile(READ)
        sandbox = BASE.sandbox_dir
        self.assertEqual(text.count("(allow file-write*"), 1, msg="a single write allowance")
        self.assertIn(f'(subpath "{sandbox}/{SCRATCH}")', text, msg="tmp writable")
        self.assertIn(f'(subpath "{sandbox}/zdot")', text, msg="zdot writable")
        self.assertNotIn(f'(allow file-write* (subpath "{sandbox}")', text, msg="no sandbox grant")

    def test_the_worktree_is_denied_after_every_write_allowance(self) -> None:
        """The later deny wins, whatever an allowance above it might cover."""
        text = render_profile(READ)
        deny = f'(deny file-write* (subpath "{BASE.worktree_dir}"))'
        self.assertIn(deny, text, msg="explicit worktree deny")
        self.assertGreater(text.index(deny), text.rindex("(allow file-write*"), msg="comes last")

    def test_a_write_task_has_no_worktree_deny_and_keeps_the_sandbox_grant(self) -> None:
        """The write profile is unchanged by the read mode."""
        text = render_profile(BASE)
        self.assertNotIn(f'(deny file-write* (subpath "{BASE.worktree_dir}"))', text, msg="no deny")
        self.assertIn(f'(allow file-write* (subpath "{BASE.sandbox_dir}")', text, msg="grant")


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


FIVE = (
    "com.apple.system.opendirectoryd.libinfo",
    "com.apple.SystemConfiguration.configd",
    "com.apple.trustd",
    "com.apple.trustd.agent",
    "com.apple.securityd.xpc",
)


class ProviderProfileTests(unittest.TestCase):
    """The Mach lookup list and the vendor-state writes belong to the provider (ARCH L3839)."""

    def test_claude_code_keeps_exactly_the_security_service(self) -> None:
        """Nothing changes for the provider that shipped first."""
        text = render_profile(BASE)
        self.assertEqual(
            text.count("(allow mach-lookup"), 1, msg="one Mach allowance for Claude Code"
        )
        self.assertIn('(global-name "com.apple.SecurityServer")', text, msg="security service")
        for name in FIVE:
            self.assertNotIn(name, text, msg=f"{name} stays denied for Claude Code")

    def test_codex_adds_the_five_services_pr3_needed(self) -> None:
        """PR3 measured exactly these five besides the security service."""
        self.assertEqual(
            CODEX_MACH_SERVICES, ("com.apple.SecurityServer", *FIVE), msg="the Codex list"
        )
        text = render_profile(replace(BASE, provider=codex_profile()))
        for name in FIVE:
            self.assertIn(f'(global-name "{name}")', text, msg=f"{name} allowed for Codex")

    def test_the_codex_profile_ships_no_vendor_state_write_until_measured(self) -> None:
        """KICKOFF-9: a guess is not shipped; the minimal set comes from the measurement."""
        self.assertEqual(codex_profile().vendor_writes, (), msg="empty until measured")

    def test_vendor_writes_are_rendered_after_the_broad_deny(self) -> None:
        """Literal, subpath and regex grants; the later allowance wins over the deny."""
        provider = ProviderProfile(
            mach_services=("com.apple.SecurityServer",),
            vendor_writes=(
                VendorWrite("subpath", "/Users/u/.codex/tmp"),
                VendorWrite("literal", "/Users/u/.codex/installation_id"),
                VendorWrite("regex", r"^/Users/u/\.codex/[a-z]+_[0-9]+\.sqlite(-wal|-shm)?$"),
            ),
        )
        text = render_profile(replace(BASE, provider=provider))
        grant = text.index('(subpath "/Users/u/.codex/tmp")')
        self.assertGreater(grant, text.index("(deny file-write*)"), msg="after the broad deny")
        self.assertIn('(literal "/Users/u/.codex/installation_id")', text, msg="literal grant")
        self.assertIn(r'(regex #"^/Users/u/\.codex/[a-z]+_[0-9]+\.sqlite(-wal|-shm)?$")', text)

    def test_a_read_task_keeps_the_worktree_deny_last_whatever_the_grants(self) -> None:
        """A grant never reopens the worktree for a read task."""
        provider = ProviderProfile(
            mach_services=("com.apple.SecurityServer",),
            vendor_writes=(VendorWrite("subpath", "/Users/u/.codex/tmp"),),
        )
        text = render_profile(replace(READ, provider=provider))
        deny = f'(deny file-write* (subpath "{BASE.worktree_dir}"))'
        self.assertGreater(text.index(deny), text.rindex("(allow file-write*"), msg="deny last")

    def test_a_grant_that_cannot_be_quoted_or_is_not_absolute_is_refused(self) -> None:
        """A relative path, a quote in a regex and a control character never reach the profile."""
        for write in (
            VendorWrite("subpath", "relative/path"),
            VendorWrite("regex", '^/a"b$'),
            VendorWrite("regex", "/not-anchored"),
            VendorWrite("literal", "/a\nb"),
            VendorWrite("glob", "/a"),
        ):
            provider = ProviderProfile(("com.apple.SecurityServer",), (write,))
            with self.subTest(write=write), self.assertRaises(ValueError, msg=repr(write)):
                render_profile(replace(BASE, provider=provider))
