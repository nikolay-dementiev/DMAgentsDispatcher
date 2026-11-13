"""The Claude Code worker adapter (PLAN-4 section 4).

It builds the command line and the environment, reads the installed version and reads the JSON
result that `--output-format json` prints. The brief goes on standard input, never into the
argument array. The environment is built from scratch so nothing of the supervisor's shell leaks
into the worker; the proxy variables are set in both spellings and no proxy exemption is passed on.
"""

import json
import re
import subprocess
from pathlib import Path

from agents_dispatcher.application.ports import (
    EgressEndpoint,
    ExecutionOutcome,
    PreparedWorkspace,
    WorkerCommand,
    WorkerOutcome,
)

DEFAULT_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
VERSION_TIMEOUT_S = 30
PRIVATE_MODE = 0o700
_VERSION = re.compile(r"^\s*(\d+(?:\.\d+)+)")
_FIXED_FLAGS = (
    "-p",
    "--safe-mode",
    "--strict-mcp-config",
)
_TAIL_FLAGS = (
    "--no-session-persistence",
    "--output-format",
    "json",
    "--permission-mode",
    "bypassPermissions",
)


class ClaudeCodeWorker:
    """A `Worker` over the Claude Code command-line tool."""

    def __init__(self, executable: str, home: str, path: str = DEFAULT_PATH) -> None:
        """Keep the executable, the real home directory (for the CLI's own login) and PATH."""
        self._executable = executable
        self._home = home
        self._path = path

    def cli_version(self) -> str:
        """Return the installed version; raise `RuntimeError` when it cannot be read."""
        try:
            completed = subprocess.run(  # noqa: S603 - argv array, no shell
                [self._executable, "--version"],
                capture_output=True,
                text=True,
                timeout=VERSION_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            message = f"cannot run {self._executable} --version: {error}"
            raise RuntimeError(message) from error
        match = _VERSION.match(completed.stdout)
        if completed.returncode != 0 or match is None:
            message = (
                f"cannot read the version of {self._executable}: "
                f"exit {completed.returncode}, output {completed.stdout.strip()[:200]!r}"
            )
            raise RuntimeError(message)
        return match.group(1)

    def command(
        self, task: str, workspace: PreparedWorkspace, egress: EgressEndpoint
    ) -> WorkerCommand:
        """Build the command and prepare the files and folders it points at."""
        sandbox = Path(workspace.sandbox_dir)
        tmp = sandbox / "tmp"
        zdot = sandbox / "zdot"
        mcp_config = sandbox / "mcp-empty.json"
        tmp.mkdir(mode=PRIVATE_MODE, exist_ok=True)
        zdot.mkdir(mode=PRIVATE_MODE, exist_ok=True)
        mcp_config.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
        proxy = f"http://{egress.host}:{egress.port}"
        environment = {
            "HOME": self._home,
            "PATH": self._path,
            "TMPDIR": str(tmp),
            "CLAUDE_CODE_TMPDIR": str(tmp),
            "ZDOTDIR": str(zdot),
            "HTTPS_PROXY": proxy,
            "HTTP_PROXY": proxy,
            "https_proxy": proxy,
            "http_proxy": proxy,
        }
        argv = (
            self._executable,
            *_FIXED_FLAGS,
            "--mcp-config",
            str(mcp_config),
            *_TAIL_FLAGS,
        )
        return WorkerCommand(
            argv=argv, environment=environment, stdin=task, cwd=workspace.worktree_dir
        )

    def parse(self, outcome: ExecutionOutcome) -> WorkerOutcome:
        """Read the result object; anything but a clean success is a failure with its text."""
        raw = outcome.stdout.strip()
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            return WorkerOutcome(
                succeeded=False, result_text=f"the worker printed no JSON result: {raw[:2000]}"
            )
        if not isinstance(body, dict) or "is_error" not in body or "result" not in body:
            return WorkerOutcome(
                succeeded=False,
                result_text=f"the worker printed an unexpected result: {raw[:2000]}",
            )
        text = str(body["result"])
        if outcome.exit_status != 0:
            status = f"the worker exited with status {outcome.exit_status}"
            return WorkerOutcome(succeeded=False, result_text=f"{status}: {text}")
        return WorkerOutcome(succeeded=body["is_error"] is False, result_text=text)
