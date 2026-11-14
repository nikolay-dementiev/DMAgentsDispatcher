"""Runs the Git executable with the SDK's hygiene rules.

Every call is an argument array, never a shell line. Hooks and the file-system monitor of the
user's repository are switched off, optional index locks are off, and prompts are disabled, so a
run can neither execute repository-supplied code through Git nor wait for a terminal.
"""

import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass

from agents_dispatcher.application.errors import WorkspaceError

HYGIENE_ARGS = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false")
DEFAULT_TIMEOUT_S = 120.0


@dataclass(frozen=True)
class GitResult:
    """The outcome of one Git call."""

    returncode: int
    stdout: bytes
    stderr: bytes

    @property
    def stderr_line(self) -> str:
        """Return the first line of Git's complaint, for error messages."""
        lines = self.stderr.decode("utf-8", errors="replace").strip().splitlines()
        return lines[0] if lines else "no message"


class GitCli:
    """A thin, injectable wrapper around the Git executable."""

    def __init__(self, executable: str = "git", timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        """Keep the executable name or path and the per-call timeout."""
        self._executable = executable
        self._timeout_s = timeout_s

    def run(
        self,
        cwd: str,
        *args: str,
        stdin: bytes | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = True,
    ) -> GitResult:
        """Run `git <hygiene> <args>` in `cwd`; a failure raises unless `check` is False."""
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", ""),
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
            **(env or {}),
        }
        try:
            completed = subprocess.run(  # noqa: S603 - argument array, no shell
                [self._executable, *HYGIENE_ARGS, *args],
                cwd=cwd,
                input=stdin,
                capture_output=True,
                env=environment,
                timeout=self._timeout_s,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            message = f"git {args[0] if args else ''} could not run: {error}"
            raise WorkspaceError(message) from error
        result = GitResult(completed.returncode, completed.stdout, completed.stderr)
        if check and result.returncode != 0:
            message = f"git {args[0] if args else ''} failed: {result.stderr_line}"
            raise WorkspaceError(message)
        return result
