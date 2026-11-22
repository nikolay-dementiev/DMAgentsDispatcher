"""The MCP tool definitions: names, descriptions, input and output schemas.

Both schemas of every tool are objects at the top level, because the protocol requires it. The
output of a tool is one of its success shapes (`oneOf`) or the failure shape, so a client can
tell them apart by their members alone.
"""

from agents_dispatcher.domain import routing, run

Schema = dict[str, object]

RUN_ID_PATTERN = f"^{run.RUN_ID_PATTERN}$"
NAME_PATTERN = f"^{routing.NAME_PATTERN}$"

# The failure codes a caller may see; the table of the command line adds its own.
INVALID_ARGUMENT = "INVALID_ARGUMENT"


def _object(properties: dict[str, Schema], required: list[str], *, closed: bool = True) -> Schema:
    """Return an object schema; closed objects refuse unknown members."""
    schema: Schema = {"type": "object", "properties": properties, "required": required}
    if closed:
        schema["additionalProperties"] = False
    return schema


def _text(**bounds: object) -> Schema:
    """Return a string schema with the given bounds."""
    return {"type": "string", **bounds}


_RUN_ID = _text(pattern=RUN_ID_PATTERN, description="the run id returned when the run started")
_ANY_OBJECT: Schema = {"type": "object"}

_ERROR = _object(
    {
        "error": _object({"code": _text(minLength=1), "message": _text()}, ["code", "message"]),
        "rejection": _ANY_OBJECT,
    },
    ["error"],
)

_STARTED = _object({"run_id": _text(), "state": _text()}, ["run_id", "state"])
_RECOMMENDATION = _object(
    {"decision": {"type": "string", "enum": ["RECOMMENDATION"]}},
    ["decision"],
    closed=False,
)
_STATUS = _object(
    {
        "target": _ANY_OBJECT,
        "revision": {"type": "integer"},
        "run": _ANY_OBJECT,
        "route": {"type": "array"},
        "owner_alive": {"type": "boolean"},
        "stop": {},
        "quarantine": _object(
            {
                "problem": _text(),
                "since": {"type": "number"},
                "survivors": {"type": "array"},
                "action": _text(),
            },
            ["problem", "since", "survivors", "action"],
        ),
    },
    ["target", "revision", "run", "route", "owner_alive", "stop"],
)
_WAIT = _object(
    {"result": {"type": "string", "enum": ["READY", "PENDING"]}, "status": _STATUS},
    ["result", "status"],
)
_STOP = _object(
    {"run_id": _text(), "disposition": _text(), "primary_cause": {}},
    ["run_id", "disposition", "primary_cause"],
)
_REPORT_PENDING = _object(
    {"result": {"type": "string", "enum": ["PENDING"]}, "status": {}}, ["result", "status"]
)
_REPORT_DONE = _object(
    {"run_id": _text(), "state": _text(), "terminal_reason": {}},
    ["run_id", "state", "terminal_reason"],
    closed=False,
)


def _output(*variants: Schema) -> Schema:
    """Return the output schema: exactly one of the success shapes or the failure shape."""
    return {"type": "object", "oneOf": [*variants, _ERROR]}


def _submission_properties() -> dict[str, Schema]:
    """Return the input members route and start share."""
    return {
        "repo": _text(minLength=1, description="path inside a local Git working tree"),
        "task": _text(minLength=1, description="the task text"),
        "task_type": _text(
            pattern=NAME_PATTERN, description="sets the mode and the provider chain"
        ),
        "hard_s": {"type": "number", "exclusiveMinimum": 0},
        "memory_bytes": {"type": "integer", "minimum": 1, "maximum": run.MAX_MEMORY_BYTES},
    }


def tool_definitions(wait_max_s: float) -> list[dict[str, object]]:
    """Return the six tool definitions, in listing order."""
    route_input = _object(_submission_properties(), ["repo", "task"])
    start_properties = {**_submission_properties(), "provider": _text(pattern=NAME_PATTERN)}
    start_input = _object(start_properties, ["repo", "task", "provider"])
    run_input = _object({"run_id": _RUN_ID}, ["run_id"])
    wait_input = _object(
        {
            "run_id": _RUN_ID,
            "timeout_s": {"type": "number", "exclusiveMinimum": 0, "maximum": wait_max_s},
        },
        ["run_id"],
    )
    stop_input = _object({"run_id": _RUN_ID, "reason": _text(minLength=1)}, ["run_id", "reason"])
    submission = _output(_STARTED, _RECOMMENDATION)
    return [
        {
            "name": "route",
            "description": (
                "Submit a task and let the task type's provider chain choose the worker. "
                "Returns the started run, or a recommendation when every link refused."
            ),
            "inputSchema": route_input,
            "outputSchema": submission,
        },
        {
            "name": "start",
            "description": "Submit a task to one named provider, with no fallback.",
            "inputSchema": start_input,
            "outputSchema": submission,
        },
        {
            "name": "status",
            "description": "Show a run right now. It never waits and never changes the run.",
            "inputSchema": run_input,
            "outputSchema": _output(_STATUS),
        },
        {
            "name": "wait",
            "description": (
                f"Wait for a run to end, at most timeout_s seconds (default and maximum "
                f"{wait_max_s:g}). A result of PENDING means call again."
            ),
            "inputSchema": wait_input,
            "outputSchema": _output(_WAIT),
        },
        {
            "name": "stop",
            "description": "Ask a run to stop, with a reason.",
            "inputSchema": stop_input,
            "outputSchema": _output(_STOP),
        },
        {
            "name": "report",
            "description": "Return the stored report of a finished run, or PENDING and its status.",
            "inputSchema": run_input,
            "outputSchema": _output(_REPORT_PENDING, _REPORT_DONE),
        },
    ]
