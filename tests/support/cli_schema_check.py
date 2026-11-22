"""Check that what a command printed conforms to the schema of the command that printed it."""

import json
from collections.abc import Sequence

from agents_dispatcher.application.schema_check import check
from agents_dispatcher.application.tool_schemas import (
    FAILURE_SCHEMA,
    QUARANTINE_RESOLVE_SCHEMA,
    tool_definitions,
)
from tests.support.cli_exit_check import command_path

_TOOL_OF_COMMAND: dict[tuple[str, ...], tuple[str, ...]] = {
    ("start",): ("start",),
    ("status",): ("status",),
    ("wait",): ("wait",),
    ("stop",): ("stop",),
    ("report",): ("report",),
    # `run` prints the stored report, or what a wait, a recommendation or a rejection prints.
    ("run",): ("report", "wait", "start"),
}


def _output_schemas() -> dict[str, dict[str, object]]:
    """Return the output schema of every MCP tool by name."""
    found: dict[str, dict[str, object]] = {}
    for tool in tool_definitions(20.0):
        schema = tool["outputSchema"]
        assert isinstance(schema, dict)  # noqa: S101 - narrows the type
        found[str(tool["name"])] = schema
    return found


def _wants_text(argv: Sequence[str]) -> bool:
    """Say whether the last of `--json` and `--format` asks for text."""
    wanted = False
    for index, word in enumerate(argv):
        if word == "--json":
            wanted = False
        elif word == "--format" and index + 1 < len(argv):
            wanted = argv[index + 1] == "text"
    return wanted


def _explained_skip(argv: Sequence[str]) -> str | None:
    """Return why this output is not an object to check, or None when it must be one."""
    if "-h" in argv or "--help" in argv:
        return "help text"
    if "--version" in argv:
        return "the version line"
    if command_path(argv) == ("help",):
        return "help text"
    if _wants_text(argv):
        return "the text format"
    return None


def _schemas_for(argv: Sequence[str]) -> list[dict[str, object]]:
    """Return the schemas the output may conform to; the failure shape when none is named."""
    path = command_path(argv)
    if path == ("quarantine", "resolve"):
        return [QUARANTINE_RESOLVE_SCHEMA]
    names = _TOOL_OF_COMMAND.get(path)
    if names is None:
        return [FAILURE_SCHEMA]
    schemas = _output_schemas()
    return [schemas[name] for name in names]


def assert_conforms(argv: Sequence[str], stdout: str) -> None:
    """Fail when stdout is not one JSON object that one schema of the command accepts."""
    if _explained_skip(argv) is not None:
        return
    try:
        document = json.loads(stdout)
    except ValueError as error:
        message = f"{' '.join(argv)} printed output that is not JSON: {stdout!r}"
        raise AssertionError(message) from error
    violations = [check(schema, document) for schema in _schemas_for(argv)]
    if all(violation is not None for violation in violations):
        message = f"{' '.join(argv)} printed an object that no schema names: {violations[0]}"
        raise AssertionError(message)
