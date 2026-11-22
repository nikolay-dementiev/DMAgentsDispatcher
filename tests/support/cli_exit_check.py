"""Check, when a code is observed, that the command's catalogue entry lists it."""

from collections.abc import Sequence

from agents_dispatcher.interfaces.cli.app import build_parser, command_parsers
from agents_dispatcher.interfaces.cli.catalogue import CATALOGUE


def command_path(argv: Sequence[str]) -> tuple[str, ...]:
    """Return the command path the arguments name; the root's empty path when none."""
    parsers = command_parsers(build_parser())
    path: tuple[str, ...] = ()
    for token in argv:
        if (*path, token) in parsers:
            path = (*path, token)
    return path


def assert_listed(argv: Sequence[str], code: int) -> None:
    """Fail when `code` is not in the exit-code list of the command `argv` ran."""
    path = command_path(argv)
    entry = CATALOGUE[path]
    if code not in entry.exit_codes:
        name = " ".join(path) or "agentsd"
        message = f"{name} returned exit code {code}, which its catalogue entry does not list"
        raise AssertionError(message)
