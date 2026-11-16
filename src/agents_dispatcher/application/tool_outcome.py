"""What a tool call comes to: a payload for the caller, and whether it is a failure."""

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolOutcome:
    """The result of one tool call.

    `payload` is the public object (the same one the command line prints, or a failure object
    with `code` and `message`). `is_error` is True when the call did not do what was asked:
    invalid arguments, an unknown run, a rejected submission, a failure of the SDK.
    """

    payload: dict[str, object]
    is_error: bool
