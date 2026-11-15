"""The agentsd command line: parsing, dispatch and presentation."""

import argparse
import json
from collections.abc import Callable, Sequence
from typing import NoReturn, TextIO

from agents_dispatcher.application.errors import (
    DecisionLogError,
    InputTooLargeError,
    InvalidRouteRequestError,
    LaunchLostError,
    NoProviderEligibleError,
    ReportUnavailableError,
    RunStoreError,
    StartInterruptedError,
    UnknownRunError,
    UnknownTaskTypeError,
    WorkspaceError,
    WorkspaceNotGitError,
    WriteTaskNotAllowedError,
)
from agents_dispatcher.application.run_queries import WaitResult
from agents_dispatcher.application.settings import InvalidSettingsError, SettingsError
from agents_dispatcher.domain.routing import Recommendation
from agents_dispatcher.domain.run import RunId, TaskSpec, TerminalReason
from agents_dispatcher.domain.views import RunSummary, escape_controls
from agents_dispatcher.interfaces.cli.services import (
    InterruptScope,
    ServiceFactory,
    Services,
    VersionSource,
)

PROGRAM = "agentsd"

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_INVALID_INPUT = 2
EXIT_PENDING = 3
EXIT_REJECTED = 20
EXIT_UNKNOWN_RUN = 21
EXIT_AUTHORITY_VIOLATION = 24
EXIT_REPORT_UNAVAILABLE = 25
# One code per terminal reason, in the architecture's 10-19 range (decision Q2 of PLAN-4).
EXIT_BY_REASON = {
    TerminalReason.COMPLETED: EXIT_OK,
    TerminalReason.FAILED: 10,
    TerminalReason.STOPPED: 13,
    TerminalReason.ORPHANED: 14,
    TerminalReason.RESOURCE_LIMIT: 16,
    TerminalReason.LAUNCH_LOST: 17,
}
EXIT_LAUNCH_LOST = EXIT_BY_REASON[TerminalReason.LAUNCH_LOST]
MESSAGE_RESULT_CHARS = 300
# `run` waits in slices so a signal is noticed within about a second.
WAIT_SLICE_S = 1.0
INTERRUPT_REASON = "interrupted"
SECOND_INTERRUPT = 2


class _InvalidInputError(Exception):
    """The arguments cannot be understood."""


class _Parser(argparse.ArgumentParser):
    """An argument parser that reports errors to the caller instead of exiting."""

    def error(self, message: str) -> NoReturn:
        raise _InvalidInputError(message)


def _run_id(text: str) -> RunId:
    """Parse a run id; anything but a canonical UUIDv4 is refused."""
    try:
        return RunId(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError(str(error)) from error


def _positive_seconds(text: str) -> float:
    """Parse a timeout in seconds; it must be a number above zero."""
    try:
        value = float(text)
    except ValueError as error:
        message = f"not a number: {text!r}"
        raise argparse.ArgumentTypeError(message) from error
    if not value > 0:
        message = f"must be above zero, got {text}"
        raise argparse.ArgumentTypeError(message)
    return value


def _add_task_arguments(command: argparse.ArgumentParser) -> None:
    command.add_argument("--repo", required=True, help="path inside a local Git working tree")
    command.add_argument("--task", required=True, help="the task text; sent on standard input")
    command.add_argument(
        "--task-type",
        help="the kind of task: sets its mode (read or write) and the providers tried, in order",
    )
    command.add_argument(
        "--provider", help="use only this provider of the task's chain, with no fallback"
    )
    command.add_argument("--hard-s", type=float, help="override the hard time limit, in seconds")
    command.add_argument("--memory-bytes", type=int, help="override the memory limit, in bytes")


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

    def command(name: str, help_text: str) -> argparse.ArgumentParser:
        return commands.add_parser(name, help=help_text, add_help=False, allow_abbrev=False)

    _add_task_arguments(
        command("start", "start one coding task in the background and print its run id")
    )
    _add_task_arguments(command("run", "start one coding task, wait for it and print the report"))
    status = command("status", "show where a run stands")
    status.add_argument("run_id", type=_run_id)
    wait = command("wait", "wait for a run to end")
    wait.add_argument("run_id", type=_run_id)
    wait.add_argument("--timeout-s", type=_positive_seconds, help="give up waiting after this long")
    stop = command("stop", "ask a run to stop")
    stop.add_argument("run_id", type=_run_id)
    stop.add_argument("--reason", required=True, help="why the run is stopped")
    report = command("report", "print the report of a finished run")
    report.add_argument("run_id", type=_run_id)
    return parser


def main(  # noqa: PLR0913, PLR0917 - the entry point receives every collaborator the commands need
    argv: Sequence[str],
    stdout: TextIO,
    stderr: TextIO,
    version_source: VersionSource,
    services: ServiceFactory,
    interrupts: InterruptScope,
) -> int:
    """Run the command and return the process exit code.

    `services` is called only for a command that needs it, so help, version and usage errors
    work with broken settings.
    """
    parser = _build_parser()
    try:
        arguments = parser.parse_args(argv)
    except _InvalidInputError as error:
        stderr.write(f"{parser.format_usage()}{PROGRAM}: error: {escape_controls(str(error))}\n")
        return EXIT_INVALID_INPUT
    early = _answer_without_a_command(arguments, parser, version_source, stdout, stderr)
    if early is not None:
        return early
    spec = None
    if arguments.command in {"start", "run"}:
        try:
            spec = TaskSpec(
                repo=arguments.repo,
                task=arguments.task,
                hard_s=arguments.hard_s,
                memory_bytes=arguments.memory_bytes,
                task_type=arguments.task_type,
                provider=arguments.provider,
            )
        except ValueError as error:
            stderr.write(escape_controls(f"{PROGRAM}: error: {error}") + "\n")
            return EXIT_INVALID_INPUT
    return _guarded(
        lambda: _dispatch(
            arguments, spec, services(), interrupts=interrupts, stdout=stdout, stderr=stderr
        ),
        stderr,
    )


def _answer_without_a_command(
    arguments: argparse.Namespace,
    parser: argparse.ArgumentParser,
    version_source: VersionSource,
    stdout: TextIO,
    stderr: TextIO,
) -> int | None:
    """Answer `--help`, `--version` and a missing command; None when a command must run."""
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
    if arguments.command is None:
        stderr.write(f"{parser.format_usage()}{PROGRAM}: error: nothing to do; try --help\n")
        return EXIT_INVALID_INPUT
    return None


_KNOWN_FAILURES: tuple[tuple[tuple[type[Exception], ...], int, str], ...] = (
    ((WorkspaceNotGitError,), EXIT_INVALID_INPUT, "WORKSPACE_NOT_GIT: "),
    ((InputTooLargeError, UnknownTaskTypeError, InvalidRouteRequestError), EXIT_INVALID_INPUT, ""),
    ((WriteTaskNotAllowedError,), EXIT_AUTHORITY_VIOLATION, "AUTHORITY_VIOLATION: "),
    ((DecisionLogError,), EXIT_FAILURE, ""),
    ((UnknownRunError,), EXIT_UNKNOWN_RUN, ""),
    ((ReportUnavailableError,), EXIT_REPORT_UNAVAILABLE, ""),
    ((InvalidSettingsError,), EXIT_INVALID_INPUT, ""),
    ((WorkspaceError, RunStoreError, SettingsError), EXIT_FAILURE, ""),
)


def _guarded(action: Callable[[], int], stderr: TextIO) -> int:
    """Run a command and turn every failure into a message and an exit code."""
    try:
        return action()
    except KeyboardInterrupt:
        stderr.write(
            f"{PROGRAM}: interrupted; if a run was created, find it with 'agentsd status'\n"
        )
        return EXIT_FAILURE
    except Exception as error:  # noqa: BLE001 - last boundary: name the failure, never a bare trace
        code, text = _explain(error)
        stderr.write(f"{PROGRAM}: error: {escape_controls(text)}\n")
        return code


def _explain(error: Exception) -> tuple[int, str]:
    """Return the exit code and the message for a failure."""
    if isinstance(error, LaunchLostError):
        return EXIT_LAUNCH_LOST, f"run {error.run_id.value} LAUNCH_LOST: {error}"
    if isinstance(error, StartInterruptedError):
        run = error.run_id.value
        return EXIT_PENDING, (
            f"run {run} was accepted and may still start in the background (it is closed as "
            f"LAUNCH_LOST if no supervisor claims it); follow it with 'agentsd wait {run}' "
            f"or end it with 'agentsd stop {run}'"
        )
    for kinds, code, prefix in _KNOWN_FAILURES:
        if isinstance(error, kinds):
            return code, f"{prefix}{error}"
    return EXIT_FAILURE, f"unexpected {type(error).__name__}: {error}"


def _dispatch(  # noqa: PLR0913 - one value per thing the commands need
    arguments: argparse.Namespace,
    spec: TaskSpec | None,
    services: Services,
    *,
    interrupts: InterruptScope,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Call the use case of the parsed command and present its answer."""
    command = arguments.command
    if command == "start" and spec is not None:
        return _start(services, spec, stdout, stderr)
    if command == "run" and spec is not None:
        return _run(services, spec, interrupts, stdout, stderr)
    if command == "status":
        _print(stdout, services.status.status(arguments.run_id).as_dict())
        return EXIT_OK
    if command == "stop":
        return _stop(services, arguments, stdout, stderr)
    if command == "wait":
        return _wait(services, arguments, interrupts, stdout, stderr)
    return _report(services, arguments.run_id, stdout)


def _print(stdout: TextIO, value: object) -> None:
    stdout.write(json.dumps(value, indent=2) + "\n")


def _submit(services: Services, spec: TaskSpec, stdout: TextIO, stderr: TextIO) -> RunSummary | int:
    """Start the task; a recommendation or a rejection is printed and becomes the exit code."""
    try:
        outcome = services.start.start(spec)
    except NoProviderEligibleError as error:
        _print(stdout, error.rejected.as_dict())
        stderr.write(f"{PROGRAM}: error: NO_PROVIDER_ELIGIBLE: {error}\n")
        return EXIT_REJECTED
    if isinstance(outcome, Recommendation):
        _print(stdout, outcome.as_dict())
        return EXIT_OK
    return outcome


def _start(services: Services, spec: TaskSpec, stdout: TextIO, stderr: TextIO) -> int:
    summary = _submit(services, spec, stdout, stderr)
    if isinstance(summary, int):
        return summary
    _print(stdout, {"run_id": summary.run_id.value, "state": summary.state.name})
    return EXIT_OK


def _stop(services: Services, arguments: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    try:
        receipt = services.stop.stop(arguments.run_id, arguments.reason)
    except ValueError as error:
        stderr.write(escape_controls(f"{PROGRAM}: error: {error}") + "\n")
        return EXIT_INVALID_INPUT
    _print(stdout, receipt.as_dict())
    return EXIT_OK


def _wait(
    services: Services,
    arguments: argparse.Namespace,
    interrupts: InterruptScope,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Wait; a signal or the timeout ends the wait and leaves the run alone."""
    with interrupts() as flag:
        result = services.wait.wait(arguments.run_id, arguments.timeout_s, flag.raised)
        interrupted = flag.raised()
    if result.ready:
        return _ready(result, stdout, stderr)
    if interrupted:
        stderr.write(f"{PROGRAM}: wait interrupted; run {arguments.run_id.value} keeps running\n")
    _print(stdout, {"result": "PENDING", "status": result.status.as_dict()})
    return EXIT_PENDING


def _ready(result: WaitResult, stdout: TextIO, stderr: TextIO) -> int:
    """Print a finished run and return the exit code of its terminal reason."""
    _print(stdout, {"result": "READY", "status": result.status.as_dict()})
    reason = result.status.run.reason
    if reason is None:
        stderr.write(f"{PROGRAM}: error: run {result.status.run.run_id.value} has no reason\n")
        return EXIT_FAILURE
    return EXIT_BY_REASON[reason]


def _report(services: Services, run_id: RunId, stdout: TextIO) -> int:
    result = services.report.report(run_id)
    if result.report_json is None:
        pending = result.pending
        _print(stdout, {"result": "PENDING", "status": pending.as_dict() if pending else None})
        return EXIT_PENDING
    stdout.write(result.report_json + "\n")
    return EXIT_OK


def _run(
    services: Services,
    spec: TaskSpec,
    interrupts: InterruptScope,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Start, wait for the end without a limit, print the stored report."""
    summary = _submit(services, spec, stdout, stderr)
    if isinstance(summary, int):
        return summary
    run_id = summary.run_id
    try:
        return _follow(services, run_id, interrupts, stdout, stderr)
    except KeyboardInterrupt as error:  # outside the interrupt scope: the run is live, name it
        message = f"interrupted while following run {run_id.value}"
        raise StartInterruptedError(run_id, message) from error


def _follow(
    services: Services,
    run_id: RunId,
    interrupts: InterruptScope,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Wait for the started run inside the interrupt scope and print its stored report."""
    stop_requested = False
    with interrupts() as flag:
        while True:
            result = services.wait.wait(run_id, WAIT_SLICE_S)
            if result.ready:
                break
            if flag.raised() and not stop_requested:
                stop_requested = True
                services.stop.stop(run_id, INTERRUPT_REASON)
            if flag.count() >= SECOND_INTERRUPT:
                stderr.write(
                    f"{PROGRAM}: run {run_id.value} keeps stopping in the background; "
                    f"follow it with 'agentsd wait {run_id.value}'\n"
                )
                _print(stdout, {"result": "PENDING", "status": result.status.as_dict()})
                return EXIT_PENDING
    reason = result.status.run.reason
    stored = services.report.report(run_id).report_json
    if stored is None or reason is None:
        stderr.write(f"{PROGRAM}: error: run {run_id.value} ended without a stored report\n")
        return EXIT_FAILURE
    stdout.write(stored + "\n")
    if reason is not TerminalReason.COMPLETED:
        stderr.write(_not_completed_message(run_id, reason, stored))
    return EXIT_BY_REASON[reason]


def _not_completed_message(run_id: RunId, reason: TerminalReason, stored: str) -> str:
    """Return the one stderr line that says why a run did not complete."""
    try:
        report = json.loads(stored)
    except ValueError:
        report = {}
    limits = report.get("limits") if isinstance(report, dict) else None
    hit = limits.get("hit") if isinstance(limits, dict) else None
    text = report.get("result_text") if isinstance(report, dict) else None
    limit = f" (limit hit: {hit})" if hit else ""
    detail = " ".join(text.split())[:MESSAGE_RESULT_CHARS] if isinstance(text, str) else ""
    suffix = f": {detail}" if detail else ""
    return (
        escape_controls(f"{PROGRAM}: run {run_id.value} ended {reason.name}{limit}{suffix}") + "\n"
    )
