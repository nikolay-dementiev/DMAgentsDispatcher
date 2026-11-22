"""The agentsd command line: parsing, dispatch and presentation."""

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, NoReturn, TextIO

from agents_dispatcher.application.errors import (
    NoProviderEligibleError,
    StartInterruptedError,
)
from agents_dispatcher.application.failures import Failure, describe
from agents_dispatcher.application.public_views import (
    pending_payload,
    quarantine_payload,
    recommendation_payload,
    rejection_payload,
    started_payload,
    status_payload,
    stop_payload,
    wait_payload,
)
from agents_dispatcher.application.quarantine_resolution import ResolutionOutcome
from agents_dispatcher.application.run_queries import WaitResult
from agents_dispatcher.application.services import ServiceFactory, Services
from agents_dispatcher.domain.routing import Recommendation
from agents_dispatcher.domain.run import RunId, RunState, TaskSpec, TerminalReason
from agents_dispatcher.domain.views import RunSummary, escape_controls
from agents_dispatcher.interfaces.cli.catalogue import CATALOGUE, render_help, render_root_help
from agents_dispatcher.interfaces.cli.exit_codes import (
    EXIT_BY_KIND,
    EXIT_BY_REASON,
    EXIT_FAILURE,
    EXIT_OK,
    EXIT_PENDING,
    EXIT_REJECTED,
)
from agents_dispatcher.interfaces.cli.failure_codes import (
    INTERRUPTED,
    MCP_UNAVAILABLE,
    VERSION_UNKNOWN,
    no_stored_report,
    refusal,
)
from agents_dispatcher.interfaces.cli.failure_output import failure_object, write_failure
from agents_dispatcher.interfaces.cli.services import InterruptScope, VersionSource

PROGRAM = "agentsd"
_HELP_HELP = "show this help and exit"
_RUN_ID_HELP = "the run id that `start` printed"

MESSAGE_RESULT_CHARS = 300
# `run` waits in slices so a signal is noticed within about a second.
WAIT_SLICE_S = 1.0
INTERRUPT_REASON = "interrupted"
SECOND_INTERRUPT = 2


class _InvalidInputError(Exception):
    """The arguments cannot be understood; `usage` is the usage line of the command that failed."""

    def __init__(self, message: str, usage: str) -> None:
        """Keep the message and the usage line that goes before it."""
        super().__init__(message)
        self.usage = usage


class _HelpRequested(Exception):  # noqa: N818 - a request, not an error
    """A `-h` or `--help` was met; `parser` is the command it belongs to."""

    def __init__(self, parser: argparse.ArgumentParser) -> None:
        """Remember which command's help was asked for."""
        super().__init__("help requested")
        self.parser = parser


class _HelpAction(argparse.Action):
    """Ask for the help of the parser that owns the flag, without exiting."""

    def __init__(self, option_strings: Sequence[str], dest: str) -> None:
        """Take no value; the flag is the whole request."""
        super().__init__(option_strings, dest, nargs=0, default=argparse.SUPPRESS, help=_HELP_HELP)

    def __call__(
        self,
        parser: argparse.ArgumentParser,
        namespace: argparse.Namespace,  # noqa: ARG002 - the signature argparse calls
        values: object,  # noqa: ARG002
        option_string: str | None = None,  # noqa: ARG002
    ) -> NoReturn:
        """Raise the request; `main` prints the help and returns 0."""
        raise _HelpRequested(parser)


class _Parser(argparse.ArgumentParser):
    """An argument parser that reports errors to the caller instead of exiting."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # noqa: ANN401 - argparse's own signature
        """Build the parser; help is plain text on every interpreter (3.14 colours it otherwise)."""
        if sys.version_info >= (3, 14):
            kwargs.setdefault("color", False)
        super().__init__(*args, **kwargs)

    def error(self, message: str) -> NoReturn:
        raise _InvalidInputError(message, self.format_usage())


def _with_help(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Give a command the flags `-h` and `--help`; they are answered while parsing."""
    parser.add_argument("-h", "--help", action=_HelpAction)
    return parser


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


def build_parser() -> argparse.ArgumentParser:
    """Build the parser of `agentsd` with every command, its help and its exit codes."""
    parser = _Parser(
        prog=PROGRAM,
        description=_description(()),
        epilog=render_root_help(CATALOGUE[()]),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
        allow_abbrev=False,
    )
    parser.add_argument("-h", "--help", action="store_true", help=_HELP_HELP)
    parser.add_argument("--version", action="store_true", help="print the version and exit")
    commands = parser.add_subparsers(dest="command", metavar="command")
    _add_task_arguments(_command(commands, ("start",)))
    _add_task_arguments(_command(commands, ("run",)))
    status = _command(commands, ("status",))
    status.add_argument("run_id", type=_run_id, help=_RUN_ID_HELP)
    wait = _command(commands, ("wait",))
    wait.add_argument("run_id", type=_run_id, help=_RUN_ID_HELP)
    wait.add_argument("--timeout-s", type=_positive_seconds, help="give up waiting after this long")
    stop = _command(commands, ("stop",))
    stop.add_argument("run_id", type=_run_id, help=_RUN_ID_HELP)
    stop.add_argument("--reason", required=True, help="why the run is stopped")
    report = _command(commands, ("report",))
    report.add_argument("run_id", type=_run_id, help=_RUN_ID_HELP)
    quarantine = _command(commands, ("quarantine",))
    quarantine_commands = quarantine.add_subparsers(dest="quarantine_command", metavar="resolve")
    resolve = _command(quarantine_commands, ("quarantine", "resolve"))
    resolve.add_argument("run_id", type=_run_id, help=_RUN_ID_HELP)
    resolve.add_argument(
        "--release",
        action="store_true",
        help="accept the risk of leftover processes and end the run (needs --confirm)",
    )
    resolve.add_argument("--confirm", help="repeat the run id to confirm a release")
    mcp = _command(commands, ("mcp",))
    mcp_commands = mcp.add_subparsers(dest="mcp_command", metavar="serve")
    _command(mcp_commands, ("mcp", "serve"))
    return parser


def _description(path: tuple[str, ...]) -> str:
    """Return the help text of a command: what the catalogue says about it."""
    entry = CATALOGUE[path]
    return entry.description if not path else render_help(entry)


def _command(actions: Any, path: tuple[str, ...]) -> argparse.ArgumentParser:  # noqa: ANN401
    """Add the parser of one command, with its summary and help from the catalogue."""
    entry = CATALOGUE[path]
    return _with_help(
        actions.add_parser(
            path[-1],
            help=entry.summary,
            description=_description(path),
            formatter_class=argparse.RawDescriptionHelpFormatter,
            add_help=False,
            allow_abbrev=False,
        )
    )


def command_parsers(
    root: argparse.ArgumentParser,
) -> dict[tuple[str, ...], argparse.ArgumentParser]:
    """Return every parser of the tree by its command path; the root has the empty path."""
    found: dict[tuple[str, ...], argparse.ArgumentParser] = {(): root}

    def walk(parser: argparse.ArgumentParser, path: tuple[str, ...]) -> None:
        for action in parser._actions:  # noqa: SLF001 - argparse has no public listing
            if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
                for name, child in action.choices.items():
                    found[(*path, name)] = child
                    walk(child, (*path, name))

    walk(root, ())
    return found


def _parse(
    parser: argparse.ArgumentParser, argv: Sequence[str], stdout: TextIO, stderr: TextIO
) -> argparse.Namespace | int:
    """Parse the arguments; a help request or a usage error is answered as an exit code."""
    try:
        arguments, extras = parser.parse_known_args(argv)
    except _HelpRequested as request:
        stdout.write(request.parser.format_help())
        return EXIT_OK
    except _InvalidInputError as error:
        return _refuse(stdout, stderr, refusal(str(error)), usage=error.usage)
    if extras:
        message = f"unrecognized arguments: {' '.join(extras)}"
        return _refuse(stdout, stderr, refusal(message), usage=_usage_of(parser, arguments))
    return arguments


def _refuse(stdout: TextIO, stderr: TextIO, failure: Failure, *, usage: str = "") -> int:
    """Print a failure: the object on stdout, `agentsd: error: <text>` on stderr, usage first."""
    text = f"{usage}{PROGRAM}: error: {escape_controls(failure.text)}\n"
    return write_failure(stdout, stderr, failure, text)


def _usage_of(root: argparse.ArgumentParser, arguments: argparse.Namespace) -> str:
    """Return the usage line of the command the arguments name; the root's when none."""
    names = (
        getattr(arguments, "command", None),
        getattr(arguments, "quarantine_command", None),
        getattr(arguments, "mcp_command", None),
    )
    path = tuple(name for name in names if name)
    return command_parsers(root)[path].format_usage()


def main(  # noqa: PLR0913, PLR0917 - the entry point receives every collaborator the commands need
    argv: Sequence[str],
    stdout: TextIO,
    stderr: TextIO,
    version_source: VersionSource,
    services: ServiceFactory,
    interrupts: InterruptScope,
    *,
    serve_mcp: Callable[[], int] | None = None,
) -> int:
    """Run the command and return the process exit code.

    `services` is called only for a command that needs it, so help, version and usage errors
    work with broken settings.
    """
    parser = build_parser()
    parsed = _parse(parser, argv, stdout, stderr)
    if isinstance(parsed, int):
        return parsed
    arguments = parsed
    early = _answer_without_a_command(arguments, parser, version_source, stdout, stderr)
    if early is not None:
        return early
    if arguments.command == "mcp":
        return _mcp(arguments, _usage_of(parser, arguments), serve_mcp, stdout, stderr)
    if arguments.command == "quarantine":
        refused = _quarantine_refusal(arguments)
        if refused is not None:
            usage = _usage_of(parser, arguments) if refused.show_usage else ""
            return _refuse(stdout, stderr, refused.failure, usage=usage)
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
            return _refuse(stdout, stderr, refusal(str(error)))
    return _guarded(
        lambda: _dispatch(
            arguments, spec, services(), interrupts=interrupts, stdout=stdout, stderr=stderr
        ),
        stderr,
        stdout,
    )


@dataclass(frozen=True, slots=True)
class _Refused:
    """A `quarantine` command that cannot run: the failure and whether its usage line goes first."""

    failure: Failure
    show_usage: bool


def _quarantine_refusal(arguments: argparse.Namespace) -> _Refused | None:
    """Return why a `quarantine` command cannot run, or None."""
    if arguments.quarantine_command != "resolve":
        return _Refused(refusal("quarantine needs a subcommand: resolve"), show_usage=True)
    if arguments.release and arguments.confirm is None:
        message = "--release needs --confirm with the run id repeated"
        return _Refused(refusal(message), show_usage=False)
    if arguments.confirm is not None and not arguments.release:
        message = "--confirm is only meaningful together with --release"
        return _Refused(refusal(message), show_usage=False)
    return None


def _mcp(
    arguments: argparse.Namespace,
    usage: str,
    serve_mcp: Callable[[], int] | None,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    """Hand over to the MCP server; it builds its own services and reads its settings once."""
    if arguments.mcp_command != "serve":
        return _refuse(stdout, stderr, refusal("mcp needs a subcommand: serve"), usage=usage)
    if serve_mcp is None:
        return _refuse(stdout, stderr, MCP_UNAVAILABLE)
    return _guarded(serve_mcp, stderr, None)


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
            return _refuse(stdout, stderr, VERSION_UNKNOWN)
        stdout.write(f"{PROGRAM} {version}\n")
        return EXIT_OK
    if arguments.command is None:
        failure = refusal("nothing to do; try --help")
        return _refuse(stdout, stderr, failure, usage=parser.format_usage())
    return None


_STILL_HELD = {ResolutionOutcome.STILL_QUARANTINED, ResolutionOutcome.STILL_UNVERIFIED}


def _guarded(action: Callable[[], int], stderr: TextIO, stdout: TextIO | None) -> int:
    """Run a command and turn every failure into a message and an exit code.

    `stdout` is None for `mcp serve`: once the server runs, standard output belongs to the
    protocol, so a failure is told on stderr only.
    """
    try:
        return action()
    except KeyboardInterrupt:
        text = f"{PROGRAM}: {INTERRUPTED.text}\n"
        return _fail(stdout, stderr, INTERRUPTED, text)
    except Exception as error:  # noqa: BLE001 - last boundary: name the failure, never a bare trace
        failure = describe(error)
        return _fail(
            stdout, stderr, failure, f"{PROGRAM}: error: {escape_controls(failure.text)}\n"
        )


def _fail(stdout: TextIO | None, stderr: TextIO, failure: Failure, stderr_text: str) -> int:
    """Tell a failure on stdout (when it is ours) and on stderr; return its exit code."""
    if stdout is None:
        stderr.write(stderr_text)
        return EXIT_BY_KIND[failure.kind]
    return write_failure(stdout, stderr, failure, stderr_text)


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
    if spec is not None:
        return (
            _start(services, spec, stdout, stderr)
            if command == "start"
            else _run(services, spec, interrupts, stdout, stderr)
        )
    if command == "status":
        _print(stdout, status_payload(services.status.status(arguments.run_id)))
        return EXIT_OK
    if command == "quarantine":
        return _quarantine(services, arguments, stdout)
    if command == "stop":
        return _stop(services, arguments, stdout, stderr)
    if command == "wait":
        return _wait(services, arguments, interrupts, stdout, stderr)
    return _report(services, arguments.run_id, stdout)


def _quarantine(services: Services, arguments: argparse.Namespace, stdout: TextIO) -> int:
    """Re-check or release a held run; a run that stays held is pending, not failed."""
    if arguments.release:
        resolution = services.quarantine.release(arguments.run_id, confirm=arguments.confirm)
    else:
        resolution = services.quarantine.resolve(arguments.run_id)
    _print(stdout, quarantine_payload(resolution))
    if resolution.outcome in _STILL_HELD:
        return EXIT_PENDING
    return EXIT_FAILURE if resolution.warnings else EXIT_OK


def _print(stdout: TextIO, value: object) -> None:
    stdout.write(json.dumps(value, indent=2) + "\n")


def _submit(services: Services, spec: TaskSpec, stdout: TextIO, stderr: TextIO) -> RunSummary | int:
    """Start the task; a recommendation or a rejection is printed and becomes the exit code."""
    try:
        outcome = services.start.start(spec)
    except NoProviderEligibleError as error:
        failure = describe(error)
        _print(stdout, failure_object(failure) | {"rejection": rejection_payload(error.rejected)})
        stderr.write(f"{PROGRAM}: error: {failure.text}\n")
        return EXIT_REJECTED
    if isinstance(outcome, Recommendation):
        _print(stdout, recommendation_payload(outcome))
        return EXIT_OK
    return outcome


def _start(services: Services, spec: TaskSpec, stdout: TextIO, stderr: TextIO) -> int:
    summary = _submit(services, spec, stdout, stderr)
    if isinstance(summary, int):
        return summary
    _print(stdout, started_payload(summary))
    return EXIT_OK


def _stop(services: Services, arguments: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    try:
        receipt = services.stop.stop(arguments.run_id, arguments.reason)
    except ValueError as error:
        return _refuse(stdout, stderr, refusal(str(error)))
    _print(stdout, stop_payload(receipt))
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
    _print(stdout, wait_payload(result))
    return EXIT_PENDING


def _ready(result: WaitResult, stdout: TextIO, stderr: TextIO) -> int:
    """Print a finished run and return the exit code of its terminal reason."""
    _print(stdout, wait_payload(result))
    reason = result.status.run.reason
    if reason is None:
        stderr.write(f"{PROGRAM}: error: run {result.status.run.run_id.value} has no reason\n")
        return EXIT_FAILURE
    return EXIT_BY_REASON[reason]


def _report(services: Services, run_id: RunId, stdout: TextIO) -> int:
    result = services.report.report(run_id)
    if result.report_json is None:
        _print(stdout, pending_payload(result.pending))
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
            if result.status.run.state is RunState.QUARANTINED:
                return _held_back(result, stdout, stderr)
            if flag.raised() and not stop_requested:
                stop_requested = True
                services.stop.stop(run_id, INTERRUPT_REASON)
            if flag.count() >= SECOND_INTERRUPT:
                stderr.write(
                    f"{PROGRAM}: run {run_id.value} keeps stopping in the background; "
                    f"follow it with 'agentsd wait {run_id.value}'\n"
                )
                _print(stdout, wait_payload(result))
                return EXIT_PENDING
    reason = result.status.run.reason
    stored = services.report.report(run_id).report_json
    if stored is None or reason is None:
        return _refuse(stdout, stderr, no_stored_report(run_id))
    stdout.write(stored + "\n")
    if reason is not TerminalReason.COMPLETED:
        stderr.write(_not_completed_message(run_id, reason, stored))
    return EXIT_BY_REASON[reason]


def _held_back(result: WaitResult, stdout: TextIO, stderr: TextIO) -> int:
    """Print a run that is quarantined and say how to resolve it; it will not end by itself."""
    _print(stdout, wait_payload(result))
    run_id = result.status.run.run_id
    stderr.write(
        f"{PROGRAM}: run {run_id.value} is held in quarantine: a worker process could not be "
        f"shown gone; resolve it with 'agentsd quarantine resolve {run_id.value}'\n"
    )
    return EXIT_PENDING


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
