"""Where a command's object goes on standard output, in the format the caller chose."""

import json
from collections.abc import Mapping
from typing import TextIO

from agents_dispatcher.interfaces.cli.text_view import render_text

JSON = "json"
TEXT = "text"
FORMATS = (JSON, TEXT)


class Output:
    """Standard output with a format: JSON for a program, text for a person."""

    def __init__(self, stream: TextIO, *, text: bool = False) -> None:
        """Keep the stream and whether objects are shown as text."""
        self._stream = stream
        self._text = text

    def emit(self, value: Mapping[str, object]) -> None:
        """Print one object."""
        if self._text:
            self._stream.write(render_text(value))
        else:
            self._stream.write(json.dumps(value, indent=2) + "\n")

    def stored(self, raw_json: str, parsed: Mapping[str, object]) -> None:
        """Print a stored report: its own text as stored in JSON, the rendered object as text."""
        if self._text:
            self._stream.write(render_text(parsed))
        else:
            self._stream.write(raw_json + "\n")
