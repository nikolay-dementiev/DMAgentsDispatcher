"""What each command says about itself: one entry per command, rendered as help and as docs."""

import json
from dataclasses import dataclass

from agents_dispatcher.interfaces.cli.exit_codes import EXIT_CODES

HELP_POINTER = "Run 'agentsd <command> --help' for the options, the output and the exit codes."
RUN_ID_SAMPLE = "0b1c2d3e-4f5a-4b6c-8d7e-9f0a1b2c3d4e"
_MEANING = {row.code: row.meaning for row in EXIT_CODES}


@dataclass(frozen=True, slots=True)
class CommandEntry:
    """One command: plain text and facts, free of any argument-parser layout."""

    path: tuple[str, ...]
    summary: str
    description: str
    output: str
    members: tuple[str, ...] | None
    exit_codes: tuple[int, ...]
    example: str
    sample: str | None = None


def exit_code_lines(codes: tuple[int, ...]) -> list[str]:
    """Return one line per code: the number and the table's meaning."""
    return [f"{code:>3}  {_MEANING[code]}" for code in codes]


def render_help(entry: CommandEntry) -> str:
    """Return the text a command's help shows below its usage line."""
    lines = [entry.description, "", f"Output: {entry.output}", "", "Exit codes:"]
    lines += exit_code_lines(entry.exit_codes)
    lines += ["", "Example:", f"  {entry.example}"]
    return "\n".join(lines)


def render_root_help(entry: CommandEntry) -> str:
    """Return the text below the root's description: the whole table and the pointer."""
    lines = ["Exit codes:", *exit_code_lines(tuple(row.code for row in EXIT_CODES))]
    lines += ["", HELP_POINTER, "", "Example:", f"  {entry.example}"]
    return "\n".join(lines)


def _json(value: object) -> str:
    """Return a sample as the pretty-printed JSON a command prints."""
    return json.dumps(value, indent=2)


_RUN = {
    "run_id": RUN_ID_SAMPLE,
    "task": "explain what main() does",
    "task_type": "review",
    "mode": "read",
    "provider": "anthropic",
    "state": "RUNNING",
    "reason": None,
    "branch": None,
}
_ROUTE = [
    {
        "provider": "anthropic",
        "disposition": "SELECTED",
        "problems": [],
        "model": "claude-sonnet-5-5",
        "effort": "medium",
    }
]
_STATUS = {
    "target": {"run_id": RUN_ID_SAMPLE},
    "revision": 3,
    "run": _RUN,
    "route": _ROUTE,
    "owner_alive": True,
    "stop": None,
}
_REPORT = {
    "run_id": RUN_ID_SAMPLE,
    "state": "TERMINAL",
    "terminal_reason": "COMPLETED",
    "problem": None,
    "task_type": "review",
    "mode": "read",
    "provider": "anthropic",
    "repo": "/work/project",
    "base_commit": "0123456789abcdef0123456789abcdef01234567",
    "branch": None,
    "commits_on_branch": 0,
    "worker": {
        "cli_version": "2.1.304",
        "exit_status": 0,
        "model": "claude-sonnet-5-5",
        "model_source": "reported",
    },
    "route": _ROUTE,
    "duration_s": 12.5,
    "limits": {"configured": {"hard_s": 3600.0, "memory_bytes": 4294967296}, "hit": None},
    "survivors": [],
    "result_text": "main() parses the arguments and starts the loop.",
    "notes": [],
}
_REPORT_MEMBERS = tuple(_REPORT)

_TASK_OUTPUT = (
    "one JSON object with the members run_id and state; when no provider takes the task, a "
    "recommendation object (exit 0) or, on exit 20, the failure object with the rejection "
    "beside it instead"
)
_REPORT_OUTPUT = (
    "the stored report of the run as one JSON object; for a run that has not ended, "
    'an object with "result": "PENDING" and the status'
)
_GROUP_OUTPUT = "nothing; the command needs a subcommand"

CATALOGUE: dict[tuple[str, ...], CommandEntry] = {
    entry.path: entry
    for entry in (
        CommandEntry(
            path=(),
            summary="",
            description="Run coding-agent CLIs as supervised background workers.",
            output="one JSON object on standard output for every command except `mcp serve`",
            members=None,
            exit_codes=(0, 1, 2),
            example="agentsd --version",
        ),
        CommandEntry(
            path=("start",),
            summary="start one coding task in the background and print its run id",
            description=(
                "Start one coding task in a supervised background run and return at once with its "
                "run id; follow it with `status`, `wait` or `report`."
            ),
            output=_TASK_OUTPUT,
            members=("run_id", "state"),
            exit_codes=(0, 1, 2, 3, 17, 20, 24),
            example='agentsd start --repo ./project --task "explain what main() does"',
            sample=_json({"run_id": RUN_ID_SAMPLE, "state": "STARTING"}),
        ),
        CommandEntry(
            path=("run",),
            summary="start one coding task, wait for it and print the report",
            description=(
                "Start one coding task, wait for the run to end and print its stored report. "
                "The exit code is the one of the run's end state."
            ),
            output=_REPORT_OUTPUT,
            members=_REPORT_MEMBERS,
            exit_codes=(0, 1, 2, 3, 10, 13, 14, 16, 17, 20, 24, 25),
            example='agentsd run --repo ./project --task "explain what main() does"',
            sample=_json(_REPORT),
        ),
        CommandEntry(
            path=("status",),
            summary="show where a run stands",
            description="Show where a run stands now; the exit code is 0 for every run state.",
            output=(
                "one JSON object with the members target, revision, run, route, owner_alive and "
                "stop, and a quarantine member while the run is held"
            ),
            members=("target", "revision", "run", "route", "owner_alive", "stop"),
            exit_codes=(0, 1, 2, 21),
            example=f"agentsd status {RUN_ID_SAMPLE}",
            sample=_json(_STATUS),
        ),
        CommandEntry(
            path=("wait",),
            summary="wait for a run to end",
            description=(
                "Wait until a run ends or the timeout passes; a signal ends the wait and leaves "
                "the run alone. The exit code is the one of the run's end state."
            ),
            output='one JSON object with the members result ("READY" or "PENDING") and status',
            members=("result", "status"),
            exit_codes=(0, 1, 2, 3, 10, 13, 14, 16, 17, 21),
            example=f"agentsd wait {RUN_ID_SAMPLE} --timeout-s 60",
            sample=_json({"result": "PENDING", "status": _STATUS}),
        ),
        CommandEntry(
            path=("stop",),
            summary="ask a run to stop",
            description="Ask a run to stop and say why; the answer says what the request did.",
            output="one JSON object with the members run_id, disposition and primary_cause",
            members=("run_id", "disposition", "primary_cause"),
            exit_codes=(0, 1, 2, 21),
            example=f'agentsd stop {RUN_ID_SAMPLE} --reason "no longer needed"',
            sample=_json(
                {"run_id": RUN_ID_SAMPLE, "disposition": "EFFECTIVE", "primary_cause": None}
            ),
        ),
        CommandEntry(
            path=("report",),
            summary="print the report of a finished run",
            description="Print the stored report of a run that has ended.",
            output=_REPORT_OUTPUT,
            members=_REPORT_MEMBERS,
            exit_codes=(0, 1, 2, 3, 21, 25),
            example=f"agentsd report {RUN_ID_SAMPLE}",
            sample=_json(_REPORT),
        ),
        CommandEntry(
            path=("quarantine",),
            summary="re-check or release a run held for leftover processes",
            description=(
                "Work with a run that is held (QUARANTINED) because a worker process could not be "
                "shown gone. The subcommand `resolve` looks at it again."
            ),
            output=_GROUP_OUTPUT,
            members=None,
            exit_codes=(0, 2),
            example=f"agentsd quarantine resolve {RUN_ID_SAMPLE}",
        ),
        CommandEntry(
            path=("quarantine", "resolve"),
            summary=(
                "look again at a held run (a process that started outside the run's life, "
                "before it or after its last sign of life, is never signalled); "
                "with --release and --confirm, release it"
            ),
            description=(
                "Look again at a held run. A process that started outside the run's life, before "
                "the run or after its last sign of life, is never signalled. With --release and "
                "--confirm (the run id repeated) the run is released at your risk."
            ),
            output="one JSON object with the members run_id, outcome, survivors and warnings",
            members=("run_id", "outcome", "survivors", "warnings"),
            exit_codes=(0, 1, 2, 3, 21),
            example=f"agentsd quarantine resolve {RUN_ID_SAMPLE}",
            sample=_json(
                {"run_id": RUN_ID_SAMPLE, "outcome": "SETTLED", "survivors": [], "warnings": []}
            ),
        ),
        CommandEntry(
            path=("mcp",),
            summary="serve the tools to an MCP client over standard input and output",
            description="Serve the agentsd tools to an MCP client; the subcommand is `serve`.",
            output=_GROUP_OUTPUT,
            members=None,
            exit_codes=(0, 2),
            example="agentsd mcp serve",
        ),
        CommandEntry(
            path=("help",),
            summary="show the help of a command",
            description=(
                "Show the help of a command, the same text as `agentsd <command> --help`; "
                "without a command, the help of agentsd itself."
            ),
            output="the help text of the command, as its --help prints it",
            members=None,
            exit_codes=(0, 2),
            example="agentsd help status",
        ),
        CommandEntry(
            path=("mcp", "serve"),
            summary="serve until the client closes the input",
            description=(
                "Serve the tools over standard input and output until the client closes the input."
            ),
            output="MCP messages on standard output, not JSON for a person",
            members=None,
            exit_codes=(0, 1, 2),
            example="agentsd mcp serve",
        ),
    )
}
