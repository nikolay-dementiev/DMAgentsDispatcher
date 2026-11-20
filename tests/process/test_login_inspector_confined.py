"""The login inspector over the real Seatbelt confinement, with fake status executables.

The fakes print what the real `codex login status` and `claude auth status` print (see the
scrubbed captures in `tests/fixtures`); the file system judges the confinement, not the fake.
"""

import sys
import tempfile
import unittest
from pathlib import Path

from agents_dispatcher.adapters.system.login_inspector import ConfinedLoginInspector
from agents_dispatcher.adapters.system.login_probes import ClaudeLoginProbe, CodexLoginProbe
from agents_dispatcher.adapters.system.seatbelt_profile import codex_profile
from agents_dispatcher.adapters.system.uuid_ids import UuidRunIds
from agents_dispatcher.domain.login import LoginState
from agents_dispatcher.domain.run import Limits
from tests.support.confinement_kit import real_confinement
from tests.support.fake_executable import FakeExecutable, SourceRef
from tests.support.hard_timeout import HardTimeoutCase

CODEX_SOURCE = SourceRef(
    kind="captured_output",
    cli_name="codex",
    cli_version="0.160.0",
    excerpt="Logged in using ChatGPT",
)
CLAUDE_SOURCE = SourceRef(
    kind="captured_output",
    cli_name="claude",
    cli_version="2.1.304",
    excerpt='{"loggedIn": false}',
)


class ConfinedInspectionTests(HardTimeoutCase):
    """Real sandbox-exec, real children."""

    def setUp(self) -> None:
        """Create folders and an inspector over the real confinement."""
        super().setUp()
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name).resolve()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        self.scratch = self.root / "scratch"
        self.inspector = self.make_inspector(Limits(hard_s=20.0, memory_bytes=1_073_741_824))

    def make_inspector(self, limits: Limits) -> ConfinedLoginInspector:
        """Build an inspector with the Codex profile (the larger of the two)."""
        confinement = real_confinement(
            interpreter=sys.executable,
            provider=codex_profile(),
            term_grace_s=0.5,
            kill_wait_s=1.0,
        )
        return ConfinedLoginInspector(
            confinement=confinement,
            ids=UuidRunIds(),
            scratch_root=str(self.scratch),
            limits=limits,
            home=str(self.home),
            user="example",
            path="/usr/bin:/bin",
        )

    def test_a_codex_status_line_is_read_through_the_real_confinement(self) -> None:
        """Logged in, exit 0."""
        tool = FakeExecutable("codex", 'echo "Logged in using ChatGPT"\n', CODEX_SOURCE)
        observation = self.inspector.observe(CodexLoginProbe(str(tool.write_to(self.bin))))
        self.assertEqual(observation.state, LoginState.LOGGED_IN, msg=observation.note)

    def test_a_claude_status_document_is_read_through_the_real_confinement(self) -> None:
        """Logged out, exit 1."""
        tool = FakeExecutable("claude", "echo '{\"loggedIn\": false}'\nexit 1\n", CLAUDE_SOURCE)
        observation = self.inspector.observe(ClaudeLoginProbe(str(tool.write_to(self.bin))))
        self.assertEqual(observation.state, LoginState.LOGGED_OUT, msg=observation.note)

    def test_a_hostile_status_executable_cannot_write_outside_its_sandbox(self) -> None:
        """E19/E20 in miniature: it claims a login but its write into the home is denied."""
        target = self.home / "pwned"
        body = f'echo "Logged in using ChatGPT"\n(echo x > "{target}") 2>/dev/null\nexit 0\n'
        tool = FakeExecutable("codex", body, CODEX_SOURCE)
        self.inspector.observe(CodexLoginProbe(str(tool.write_to(self.bin))))
        self.assertFalse(target.exists(), msg="the write outside the sandbox was denied")

    def test_a_status_command_that_runs_too_long_is_unknown(self) -> None:
        """The hard limit ends it and the observation is not trusted."""
        inspector = self.make_inspector(Limits(hard_s=1.0, memory_bytes=1_073_741_824))
        tool = FakeExecutable("codex", "sleep 30\n", CODEX_SOURCE)
        observation = inspector.observe(CodexLoginProbe(str(tool.write_to(self.bin))))
        self.assertEqual(observation.state, LoginState.UNKNOWN, msg=observation.note)

    def test_nothing_is_left_in_the_scratch_folder(self) -> None:
        """Every sandbox is removed."""
        tool = FakeExecutable("codex", 'echo "Logged in using ChatGPT"\n', CODEX_SOURCE)
        self.inspector.observe(CodexLoginProbe(str(tool.write_to(self.bin))))
        self.assertEqual(list(self.scratch.iterdir()), [], msg="scratch is empty")


if __name__ == "__main__":
    unittest.main()
