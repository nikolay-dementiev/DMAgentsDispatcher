"""What a failing command prints: the failure object on stdout, a line for a person on stderr."""

from typing import TextIO

from agents_dispatcher.application.failures import Failure
from agents_dispatcher.interfaces.cli.exit_codes import EXIT_BY_KIND
from agents_dispatcher.interfaces.cli.output import Output


def failure_object(failure: Failure) -> dict[str, object]:
    """Return the object MCP also returns for the failure: its code and its message."""
    return {"error": {"code": failure.code, "message": failure.text}}


def write_failure(stdout: Output, stderr: TextIO, failure: Failure, stderr_text: str) -> int:
    """Print the failure object, then the line for a person, and return the exit code.

    The object is the only document on stdout; the exit code comes from the failure's kind, so
    the code a caller reads and the code the process returns cannot name different failures.
    """
    stdout.emit(failure_object(failure))
    stderr.write(stderr_text)
    return EXIT_BY_KIND[failure.kind]
