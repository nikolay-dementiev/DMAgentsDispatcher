"""The Codex worker adapter (PLAN-9 section 4).

It builds the command line and the environment for `codex exec --json`, reads the installed
version and reads the event stream. The brief goes on standard input, never into the argument
array. The environment is built from scratch. The CLI's own sandbox is switched off
(`-s danger-full-access`) because the SDK's outer profile is the boundary; the CLI's bypass flag is
never passed. Every feature that would reach beyond the brief or the proxy is disabled by flag.
"""

import json
import re
import subprocess
from pathlib import Path

from agents_dispatcher.adapters.system.private_folder import ensure_private_folder
from agents_dispatcher.application.errors import LaunchError
from agents_dispatcher.application.ports import (
    EgressEndpoint,
    ExecutionOutcome,
    PreparedWorkspace,
    WorkerCommand,
    WorkerInvocation,
    WorkerOutcome,
)
from agents_dispatcher.domain.views import bounded_text

DEFAULT_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
VERSION_TIMEOUT_S = 30
PRIVATE_MODE = 0o700
_VERSION = re.compile(r"^\s*codex-cli\s+(\d+(?:\.\d+)+)")
_DISABLED_FEATURES = (
    "apps",
    "plugins",
    "remote_plugin",
    "hooks",
    "shell_snapshot",
    "skill_search",
    "skill_mcp_dependency_install",
    "tool_suggest",
    "browser_use",
    "computer_use",
    "in_app_browser",
)
_FIXED_FLAGS = (
    "exec",
    "--json",
    "--ignore-user-config",
    "--ignore-rules",
    "--ephemeral",
    "--skip-git-repo-check",
    "-s",
    "danger-full-access",
    "-c",
    "apps._default.enabled=false",
    *(item for feature in _DISABLED_FEATURES for item in ("--disable", feature)),
)


class CodexWorker:
    """A `Worker` over the Codex command-line tool."""

    def __init__(
        self,
        executable: str,
        home: str,
        user: str,
        *,
        kept_bytes: int,
        path: str = DEFAULT_PATH,
    ) -> None:
        """Keep the executable, the real home and user name and PATH.

        The home is where the CLI finds its own login. `kept_bytes` bounds the raw output kept in
        a failure text (`public.summary_bytes`).
        """
        self._executable = executable
        self._kept_bytes = kept_bytes
        self._home = home
        self._user = user
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
        self,
        task: str,
        workspace: PreparedWorkspace,
        egress: EgressEndpoint,
        invocation: WorkerInvocation,
    ) -> WorkerCommand:
        """Build the command and prepare the folders it points at."""
        sandbox = Path(workspace.sandbox_dir)
        tmp = sandbox / "tmp"
        zdot = sandbox / "zdot"
        for folder in (tmp, zdot):
            ensure_private_folder(folder, PRIVATE_MODE, LaunchError)
        proxy = f"http://{egress.host}:{egress.port}"
        environment = {
            "HOME": self._home,
            "USER": self._user,
            "LOGNAME": self._user,
            "PATH": self._path,
            "TMPDIR": str(tmp),
            "ZDOTDIR": str(zdot),
            "HTTPS_PROXY": proxy,
            "HTTP_PROXY": proxy,
            "https_proxy": proxy,
            "http_proxy": proxy,
        }
        argv = (
            self._executable,
            *_FIXED_FLAGS,
            "-m",
            invocation.model,
            "-c",
            f'model_reasoning_effort="{invocation.effort}"',
            "-",
        )
        return WorkerCommand(
            argv=argv, environment=environment, stdin=task, cwd=workspace.worktree_dir
        )

    def parse(self, outcome: ExecutionOutcome) -> WorkerOutcome:
        """Read the events; only a completed turn with a nonempty final message is a success."""
        raw = outcome.stdout.strip()
        kept = bounded_text(raw, self._kept_bytes)
        events: list[dict[str, object]] = []
        for line in raw.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                return WorkerOutcome(
                    succeeded=False,
                    result_text=f"the worker printed a line that is no JSON: {kept}",
                )
            if isinstance(event, dict):
                events.append(event)
        if outcome.exit_status != 0:
            status = f"the worker exited with status {outcome.exit_status}"
            return WorkerOutcome(succeeded=False, result_text=f"{status}: {kept}")
        failure = _failure_of(events)
        if failure is not None:
            return WorkerOutcome(succeeded=False, result_text=failure)
        if not any(event.get("type") == "turn.completed" for event in events):
            return WorkerOutcome(
                succeeded=False, result_text=f"the worker ended before a turn completed: {kept}"
            )
        report = _final_message(events)
        if not report.strip():
            return WorkerOutcome(
                succeeded=False, result_text=f"the worker completed a turn with no report: {kept}"
            )
        return WorkerOutcome(succeeded=True, result_text=report)


def _failure_of(events: list[dict[str, object]]) -> str | None:
    """Return the text of the first `turn.failed` or `error` event, or None."""
    for event in events:
        kind = event.get("type")
        if kind == "turn.failed":
            error = event.get("error")
            detail = error.get("message") if isinstance(error, dict) else None
            return f"the worker's turn failed: {detail if isinstance(detail, str) else event}"
        if kind == "error":
            detail = event.get("message")
            return f"the worker reported an error: {detail if isinstance(detail, str) else event}"
    return None


def _final_message(events: list[dict[str, object]]) -> str:
    """Return the text of the last completed `agent_message` item; earlier ones are narration."""
    report = ""
    for event in events:
        item = event.get("item")
        if (
            event.get("type") == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "agent_message"
            and isinstance(item.get("text"), str)
        ):
            report = str(item["text"])
    return report
