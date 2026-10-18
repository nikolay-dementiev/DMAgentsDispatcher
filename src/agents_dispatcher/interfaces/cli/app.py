"""The agentsd command line: parsing, dispatch and presentation."""

import argparse
from collections.abc import Sequence
from typing import NoReturn, Protocol, TextIO

PROGRAM = "agentsd"

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_INVALID_INPUT = 2


class VersionSource(Protocol):
    """Reports the installed version of the distribution, if there is one."""

    def installed_version(self) -> str | None:
        """Return the installed version, or None when the metadata is missing."""
        ...


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
    return parser


def main(
    argv: Sequence[str],
    stdout: TextIO,
    stderr: TextIO,
    version_source: VersionSource,
) -> int:
    """Run the command and return the process exit code."""
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

    stderr.write(f"{parser.format_usage()}{PROGRAM}: error: nothing to do; try --help\n")
    return EXIT_INVALID_INPUT
