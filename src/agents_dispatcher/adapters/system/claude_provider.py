"""The Claude Code provider plugin: it knows where its tool is and refuses a task without it."""

from agents_dispatcher.adapters.system.tool_lookup import resolve_tool
from agents_dispatcher.application.settings import SettingsError
from agents_dispatcher.domain.routing import Problem, ProblemCode, ResolvedTask

EXECUTABLE_KEY = "profiles.anthropic.main.executable"
WORKER_NAME = "claude"


class ClaudeCodeProvider:
    """A `ProviderPlugin` over the Claude Code command-line tool."""

    def __init__(self, executable_setting: str, path: str) -> None:
        """Keep the provider's tool setting and the search path used for `auto`."""
        self._setting = executable_setting
        self._path = path

    def check(self, task: ResolvedTask) -> tuple[Problem, ...]:  # noqa: ARG002 - mode-independent
        """Refuse every task while the tool cannot be found (EXECUTABLE_UNAVAILABLE)."""
        try:
            self.executable()
        except SettingsError as error:
            return (Problem(ProblemCode.EXECUTABLE_UNAVAILABLE, str(error)),)
        return ()

    def executable(self) -> str:
        """Return the tool to run; raise `SettingsError` when it cannot be found."""
        return resolve_tool(self._setting, self._path, name=WORKER_NAME, key=EXECUTABLE_KEY)
