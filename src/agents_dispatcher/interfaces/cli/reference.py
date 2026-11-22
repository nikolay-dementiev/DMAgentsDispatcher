"""The README command section and `docs/cli.md`, rendered from the catalogue and the parsers.

Nothing here calls argparse's help formatter, so the text is the same on every interpreter.
"""

import argparse

from agents_dispatcher.interfaces.cli.app import build_parser, command_parsers
from agents_dispatcher.interfaces.cli.catalogue import CATALOGUE, CommandEntry
from agents_dispatcher.interfaces.cli.exit_codes import EXIT_CODES

README_START = "<!-- cli-section:start -->"
README_END = "<!-- cli-section:end -->"
PROGRAM = "agentsd"
OUTPUT_NOTE = (
    "`agentsd` prints its answer as one JSON object on standard output (`mcp serve` speaks MCP "
    "instead). There is no `--json` flag: JSON is the only output format. Messages for a person go "
    "to standard error, and the exit code says how the command ended."
)


def _commands() -> list[CommandEntry]:
    """Return the entries of the commands, in catalogue order, without the root."""
    return [entry for path, entry in CATALOGUE.items() if path]


def _name(entry: CommandEntry) -> str:
    return " ".join((PROGRAM, *entry.path))


def _first_sentence(text: str) -> str:
    """Return the text up to its first full stop, with the stop."""
    return text.split(". ", maxsplit=1)[0].rstrip(".") + "."


def _arguments(parser: argparse.ArgumentParser) -> list[argparse.Action]:
    """Return the actions of a parser that a user types: no help flag, no subcommand group."""
    return [
        action
        for action in parser._actions  # noqa: SLF001 - argparse has no public listing
        if not isinstance(action, argparse._SubParsersAction)  # noqa: SLF001
        and "--help" not in action.option_strings
    ]


def _placeholder(action: argparse.Action) -> str:
    return action.dest.upper()


def _usage(entry: CommandEntry, parser: argparse.ArgumentParser) -> str:
    """Build the usage line from the parser's arguments."""
    words = [_name(entry)]
    for action in _arguments(parser):
        if not action.option_strings:
            words.append(_placeholder(action))
            continue
        flag = action.option_strings[-1]
        word = flag if action.nargs == 0 else f"{flag} {_placeholder(action)}"
        words.append(word if action.required else f"[{word}]")
    return " ".join(words)


def _option_rows(parser: argparse.ArgumentParser) -> list[str]:
    """Return one table row per argument: how it is typed and what it means."""
    rows = []
    for action in _arguments(parser):
        if not action.option_strings:
            shown = _placeholder(action)
        else:
            flag = action.option_strings[-1]
            shown = flag if action.nargs == 0 else f"{flag} {_placeholder(action)}"
        required = " (required)" if action.required or not action.option_strings else ""
        rows.append(f"| `{shown}`{required} | {action.help} |")
    return rows


def _exit_code_table(codes: tuple[int, ...]) -> list[str]:
    meanings = {row.code: row.meaning for row in EXIT_CODES}
    return ["| Exit code | Meaning |", "|---|---|", *(f"| {c} | {meanings[c]} |" for c in codes)]


def _command_part(entry: CommandEntry, parser: argparse.ArgumentParser) -> list[str]:
    lines = [f"### `{_name(entry)}`", "", entry.description, ""]
    lines += [f"Usage: `{_usage(entry, parser)}`", ""]
    rows = _option_rows(parser)
    if rows:
        lines += ["| Argument | Meaning |", "|---|---|", *rows, ""]
    lines += [f"Output: {entry.output}.", ""]
    if entry.sample is not None:
        lines += ["```json", entry.sample, "```", ""]
    lines += ["Exit codes:", "", *_exit_code_table(entry.exit_codes), ""]
    lines += ["Example:", "", "```bash", entry.example, "```", ""]
    return lines


def render_reference() -> str:
    """Return the text of `docs/cli.md`."""
    parsers = command_parsers(build_parser())
    lines = [
        "# agentsd command line reference",
        "",
        (
            "This file is generated from the command catalogue. Do not edit it by hand: "
            "run `python -m tests.support.cli_docs --write` from the repository root."
        ),
        "",
        OUTPUT_NOTE,
        "",
        (
            "Every command answers `-h` and `--help` with its arguments, its output, its exit "
            "codes and an example; `agentsd --version` prints the version."
        ),
        "",
        "## Exit codes",
        "",
        *_exit_code_table(tuple(row.code for row in EXIT_CODES)),
        "",
        "## Commands",
        "",
    ]
    for entry in _commands():
        lines += _command_part(entry, parsers[entry.path])
    return "\n".join(lines).rstrip("\n") + "\n"


def render_readme_section() -> str:
    """Return the generated block of the README: one row per command, with an example."""
    lines = [
        OUTPUT_NOTE,
        "",
        "| Command | What it does | Example |",
        "|---|---|---|",
    ]
    for entry in _commands():
        summary = _first_sentence(entry.description)
        lines.append(f"| `{_name(entry)}` | {summary} | `{entry.example}` |")
    lines += [
        "",
        (
            "Every command answers `--help`. The full reference, with the JSON each command "
            "prints and its exit codes, is [docs/cli.md](docs/cli.md)."
        ),
    ]
    return "\n".join(lines)
