"""The agentsd command line: parsing, dispatch and presentation."""

import argparse
import json
from collections.abc import Callable, Sequence
from typing import NoReturn, Protocol, TextIO

from agents_dispatcher.application.errors import (
    RunStoreError,
    WorkspaceError,
    WorkspaceNotGitError,
)
from agents_dispatcher.application.settings import SettingsError
from agents_dispatcher.domain.run import RunReport, TaskSpec, TerminalReason

PROGRAM = "agentsd"

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_INVALID_INPUT = 2
# One code per terminal reason, in the architecture's 10-19 range (decision Q2 of PLAN-4).
EXIT_BY_REASON = {
    TerminalReason.COMPLETED: EXIT_OK,
    TerminalReason.FAILED: 10,
    TerminalReason.STOPPED: 13,
    TerminalReason.ORPHANED: 14,
    TerminalReason.RESOURCE_LIMIT: 16,
    TerminalReason.LAUNCH_LOST: 17,
}
MESSAGE_RESULT_CHARS = 300


class VersionSource(Protocol):
    """Reports the installed version of the distribution, if there is one."""

    def installed_version(self) -> str | None:
        """Return the installed version, or None when the metadata is missing."""
        ...


class RunService(Protocol):
    """Runs one task and reports what happened."""

    def run(self, spec: TaskSpec) -> RunReport:
        """Run the task to a terminal state."""
        ...


RunServiceFactory = Callable[[], RunService]


class _InvalidInputError(Exception):
    """The arguments cannot be understood."""


class _Parser(argparse.ArgumentParser):
    """An argument parser that reports errors to the caller instead of exiting."""

    def error(self, message: str) -> NoReturn:
        raise _InvalidInputError(message)


def _build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog=PROGRAM,
        description="Run coding-agent CLIs as supervised background workers.",
        add_help=False,
        allow_abbrev=False,
    )
    parser.add_argument("-h", "--help", action="store_true", help="show this help and exit")
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    commands = parser.add_subparsers(dest="command", metavar="command")
    run = commands.add_parser(
        "run",
        help="run one coding task in an isolated worktree and print a JSON report",
        add_help=False,
        allow_abbrev=False,
    )
    run.add_argument("--repo", required=True, help="path inside a local Git working tree")
    run.add_argument("--task", required=True, help="the task text; sent on standard input")
    run.add_argument("--hard-s", type=float, help="override the hard time limit, in seconds")
    run.add_argument("--memory-bytes", type=int, help="override the memory limit, in bytes")
    return parser


def main(
    argv: Sequence[str],
    stdout: TextIO,
    stderr: TextIO,
    version_source: VersionSource,
    run_service: RunServiceFactory,
) -> int:
    """Run the command and return the process exit code.

    `run_service` is called only for `run`, so help and version work with broken settings.
    """
    parser = _build_parser()
    try:
        arguments = parser.parse_args(argv)
    except _InvalidInputError as error:
        stderr.write(f"{parser.format_usage()}{PROGRAM}: error: {error}\n")
        return EXIT_INVALID_INPUT

    if arguments.help:
        stdout.write(parser.format_help())
        return EXIT_OK

    if arguments.version:
        version = version_source.installed_version()
        if version is None:
            stderr.write(
                f"{PROGRAM}: error: the package metadata is not installed, "
                "so the version is unknown; reinstall the package\n",
            )
            return EXIT_FAILURE
        stdout.write(f"{PROGRAM} {version}\n")
        return EXIT_OK

    if arguments.command == "run":
        return _run_task(arguments, stdout, stderr, run_service)

    stderr.write(f"{parser.format_usage()}{PROGRAM}: error: nothing to do; try --help\n")
    return EXIT_INVALID_INPUT


def _run_task(
    arguments: argparse.Namespace, stdout: TextIO, stderr: TextIO, run_service: RunServiceFactory
) -> int:
    """Run the `run` command: build the spec, run it, print the report, map the exit code."""
    try:
        spec = TaskSpec(
            repo=arguments.repo,
            task=arguments.task,
            hard_s=arguments.hard_s,
            memory_bytes=arguments.memory_bytes,
        )
    except ValueError as error:
        stderr.write(f"{PROGRAM}: error: {error}\n")
        return EXIT_INVALID_INPUT
    try:
        report = run_service().run(spec)
    except WorkspaceNotGitError as error:
        stderr.write(f"{PROGRAM}: error: WORKSPACE_NOT_GIT: {error}\n")
        return EXIT_INVALID_INPUT
    except (WorkspaceError, RunStoreError, SettingsError) as error:
        stderr.write(f"{PROGRAM}: error: {error}\n")
        return EXIT_FAILURE
    except Exception as error:  # noqa: BLE001 - last boundary: name the failure, never a bare trace
        stderr.write(f"{PROGRAM}: error: unexpected {type(error).__name__}: {error}\n")
        return EXIT_FAILURE
    stdout.write(json.dumps(report.as_dict(), indent=2) + "\n")
    reason = report.terminal_reason
    if reason is None:
        stderr.write(f"{PROGRAM}: error: run {report.run_id.value} has no terminal reason\n")
        return EXIT_FAILURE
    if reason is not TerminalReason.COMPLETED:
        stderr.write(_not_completed_message(report, reason))
    return EXIT_BY_REASON[reason]


def _not_completed_message(report: RunReport, reason: TerminalReason) -> str:
    """Return the one stderr line that says why a run did not complete."""
    limit = f" (limit hit: {report.limit_hit.value})" if report.limit_hit else ""
    detail = " ".join(report.result_text.split())[:MESSAGE_RESULT_CHARS]
    suffix = f": {detail}" if detail else ""
    return f"{PROGRAM}: run {report.run_id.value} ended {reason.name}{limit}{suffix}\n"
