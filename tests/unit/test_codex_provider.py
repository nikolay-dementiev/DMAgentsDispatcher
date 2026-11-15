"""Tests of the Codex provider plugin: it refuses a task when its tool cannot be found."""

import tempfile
import unittest
from pathlib import Path

from agents_dispatcher.adapters.system.codex_provider import CodexProvider
from agents_dispatcher.domain.routing import (
    Chain,
    ProblemCode,
    ProviderLink,
    ResolvedTask,
    TaskMode,
)
from tests.support.fake_executable import FakeExecutable, SourceRef

TASK = ResolvedTask("audit", TaskMode.READ, Chain((ProviderLink("openai"),)))
# The provider only looks the tool up; a fake shaped like `codex --version` stands in.
FAKE_CODEX = FakeExecutable(
    "codex",
    'echo "codex-cli 0.160.0"\n',
    SourceRef(
        kind="captured_output",
        cli_name="codex",
        cli_version="0.160.0",
        excerpt="codex-cli 0.160.0",
    ),
)


class CodexProviderTests(unittest.TestCase):
    """EXECUTABLE_UNAVAILABLE is decided by the provider that owns the tool."""

    def setUp(self) -> None:
        """Create a private folder with a runnable tool and an empty folder."""
        self._folder = tempfile.TemporaryDirectory()
        self.addCleanup(self._folder.cleanup)
        self.root = Path(self._folder.name)
        self.tool = FAKE_CODEX.write_to(self.root)
        (self.root / "empty").mkdir()

    def test_auto_finds_the_tool_on_the_given_path(self) -> None:
        """No problem when the search path holds a runnable codex."""
        self.assertEqual(CodexProvider("auto", str(self.root)).check(TASK), (), msg="eligible")

    def test_auto_without_the_tool_refuses_with_the_remedy(self) -> None:
        """The message names the tool and the key that fixes it."""
        (problem,) = CodexProvider("auto", str(self.root / "empty")).check(TASK)
        self.assertEqual(problem.code, ProblemCode.EXECUTABLE_UNAVAILABLE, msg="the code")
        self.assertIn("codex", problem.message, msg="the tool is named")
        self.assertIn("profiles.openai.main.executable", problem.message, msg="the remedy")

    def test_an_explicit_path_that_is_not_runnable_refuses(self) -> None:
        """No fall back to another codex."""
        provider = CodexProvider(str(self.root / "missing"), str(self.root))
        (problem,) = provider.check(TASK)
        self.assertEqual(problem.code, ProblemCode.EXECUTABLE_UNAVAILABLE, msg="the code")

    def test_the_resolved_path_is_available_for_the_worker(self) -> None:
        """One resolution rule for the check and for the worker."""
        provider = CodexProvider("auto", str(self.root))
        self.assertEqual(provider.executable(), str(self.tool), msg="the same file")
