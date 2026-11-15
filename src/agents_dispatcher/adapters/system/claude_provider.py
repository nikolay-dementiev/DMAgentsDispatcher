"""The Claude Code provider plugin: it knows where its tool is and refuses a task without it."""

import os
import shutil
from pathlib import Path

from agents_dispatcher.application.settings import SettingsError
from agents_dispatcher.domain.routing import Problem, ProblemCode, ResolvedTask

EXECUTABLE_KEY = "profiles._template.executable"
WORKER_NAME = "claude"


def resolve_executable(setting: str, path: str) -> str:
    """Return the worker executable: the configured file, or `claude` found on the search path."""
    if setting == "auto":
        found = shutil.which(WORKER_NAME, path=path)
        if found is None:
            message = (
                f"settings: {EXECUTABLE_KEY}: no {WORKER_NAME} executable found on {path}; "
                "install Claude Code or set this key to its absolute path"
            )
            raise SettingsError(message)
        return found
    candidate = Path(setting)
    try:
        usable = candidate.is_file() and os.access(candidate, os.X_OK)
    except OSError:
        usable = False  # Python before 3.14 raises for a name the file system refuses (too long)
    if not usable:
        message = f"settings: {EXECUTABLE_KEY}: {setting} is not an executable file"
        raise SettingsError(message)
    return setting


class ClaudeCodeProvider:
    """A `ProviderPlugin` over the Claude Code command-line tool."""

    def __init__(self, executable_setting: str, path: str) -> None:
        """Keep `profiles._template.executable` and the search path used for `auto`."""
        self._setting = executable_setting
        self._path = path

    def check(self, task: ResolvedTask) -> tuple[Problem, ...]:  # noqa: ARG002 - mode-independent
        """Refuse every task while the tool cannot be found (EXECUTABLE_UNAVAILABLE)."""
        try:
            resolve_executable(self._setting, self._path)
        except SettingsError as error:
            return (Problem(ProblemCode.EXECUTABLE_UNAVAILABLE, str(error)),)
        return ()

    def executable(self) -> str:
        """Return the tool to run; raise `SettingsError` when it cannot be found."""
        return resolve_executable(self._setting, self._path)
