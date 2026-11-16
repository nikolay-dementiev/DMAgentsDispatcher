"""Tests of the Claude Code provider plugin: it refuses a task when its tool cannot be found."""

import tempfile
import unittest
from pathlib import Path

from agents_dispatcher.adapters.system.claude_provider import ClaudeCodeProvider
from agents_dispatcher.domain.routing import (
    Chain,
    ProblemCode,
    ProviderLink,
    ResolvedTask,
    TaskMode,
)
from tests.support.fake_executable import FakeExecutable, SourceRef

TASK = ResolvedTask("audit", TaskMode.READ, Chain((ProviderLink("anthropic"),)))
# The provider only looks the tool up; a fake shaped like the real CLI's version output stands in.
FAKE_CLAUDE = FakeExecutable(
    "claude",
    'echo "2.1.304 (Claude Code)"\n',
    SourceRef(
        kind="captured_output",
        cli_name="claude",
        cli_version="2.1.304",
        excerpt="2.1.304 (Claude Code)",
    ),
)


class ClaudeCodeProviderTests(unittest.TestCase):
    """EXECUTABLE_UNAVAILABLE (ARCH L1388) is decided by the provider that owns the tool."""

    def setUp(self) -> None:
        """Create a private folder with a runnable tool and an empty folder."""
        self._folder = tempfile.TemporaryDirectory()
        self.addCleanup(self._folder.cleanup)
        self.root = Path(self._folder.name)
        self.tool = FAKE_CLAUDE.write_to(self.root)
        (self.root / "empty").mkdir()

    def test_auto_finds_the_tool_on_the_given_path(self) -> None:
        """No problem when the search path holds a runnable claude."""
        provider = ClaudeCodeProvider("auto", str(self.root))
        self.assertEqual(provider.check(TASK), (), msg="eligible")

    def test_auto_without_the_tool_refuses_with_the_remedy(self) -> None:
        """The message names the tool and the key that fixes it."""
        provider = ClaudeCodeProvider("auto", str(self.root / "empty"))
        (problem,) = provider.check(TASK)
        self.assertEqual(problem.code, ProblemCode.EXECUTABLE_UNAVAILABLE, msg="the code")
        self.assertIn("claude", problem.message, msg="the tool is named")
        self.assertIn(
            "profiles.anthropic.main.executable", problem.message, msg="the remedy is named"
        )

    def test_an_explicit_path_that_is_not_runnable_refuses(self) -> None:
        """No fall back to another claude."""
        provider = ClaudeCodeProvider(str(self.root / "missing"), str(self.root))
        (problem,) = provider.check(TASK)
        self.assertEqual(problem.code, ProblemCode.EXECUTABLE_UNAVAILABLE, msg="the code")

    def test_an_explicit_runnable_path_is_eligible(self) -> None:
        """The configured file wins over the search path."""
        provider = ClaudeCodeProvider(str(self.tool), str(self.root / "empty"))
        self.assertEqual(provider.check(TASK), (), msg="eligible")

    def test_the_resolved_path_is_available_for_the_worker(self) -> None:
        """One resolution rule for the check and for the worker."""
        provider = ClaudeCodeProvider("auto", str(self.root))
        self.assertEqual(provider.executable(), str(self.tool), msg="the same file")


if __name__ == "__main__":
    unittest.main()
