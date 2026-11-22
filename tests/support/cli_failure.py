"""Read the failure object a failing command prints on standard output."""

import json

from agents_dispatcher.interfaces.cli.failure_codes import ALL_CODES


def failure_of(stdout: str) -> dict[str, str]:
    """Return the `error` member of a stdout that is exactly one failure object.

    Fails when stdout holds anything else, when the object is not printed the way every command
    object is, or when its code is not in the table of codes.
    """
    document = json.loads(stdout)
    if stdout != json.dumps(document, indent=2) + "\n":
        message = f"stdout is not one indented JSON document: {stdout!r}"
        raise AssertionError(message)
    if list(document) != ["error"] or sorted(document["error"]) != ["code", "message"]:
        message = f"stdout is not a failure object: {document!r}"
        raise AssertionError(message)
    error: dict[str, str] = document["error"]
    if error["code"] not in ALL_CODES:
        message = f"the code {error['code']!r} is not in the table of failure codes"
        raise AssertionError(message)
    return error
