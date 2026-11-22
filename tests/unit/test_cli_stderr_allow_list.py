"""A failure cannot reach stderr alone: only a short list of functions writes to it.

Owner decision 2026-11-22: every failing command prints the failure object on stdout. A new
`stderr.write` in another function would be a failure that prints nothing there, so it must be
added to the list below on purpose, with the reason.
"""

import ast
import unittest
from pathlib import Path

APP = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "agents_dispatcher"
    / "interfaces"
    / "cli"
    / "app.py"
)

# function -> why its stderr write is not a failure that printed nothing on stdout
ALLOWED: dict[str, str] = {
    "_fail": "the one place a failure is told: object first, then this line",
    "_submit": "a rejection: the command prints the rejection object and keeps its stderr line",
    "_wait": "a second interrupt: the wait payload is already on stdout",
    "_ready": "a ready run without a reason: the status object is already on stdout",
    "_follow": "a run that did not complete: the report object is already on stdout",
    "_held_back": "a run held back: its object is already on stdout",
}


def stderr_writers(source: str) -> set[str]:
    """Return the names of the functions that call `stderr.write`."""
    names: set[str] = set()
    for function in ast.walk(ast.parse(source)):
        if not isinstance(function, ast.FunctionDef):
            continue
        for node in ast.walk(function):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "write"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "stderr"
            ):
                names.add(function.name)
    return names


class StderrAllowListTests(unittest.TestCase):
    """Where the command line may write to stderr."""

    def test_only_the_listed_functions_write_to_stderr(self) -> None:
        """A new writer fails here until its reason is written down."""
        found = stderr_writers(APP.read_text(encoding="utf-8"))
        self.assertEqual(found - set(ALLOWED), set(), msg="a function writes to stderr unlisted")

    def test_every_listed_function_still_writes_to_stderr(self) -> None:
        """A stale entry would let a later writer hide behind it."""
        found = stderr_writers(APP.read_text(encoding="utf-8"))
        self.assertEqual(set(ALLOWED) - found, set(), msg="a listed function no longer writes")

    def test_the_scanner_sees_an_unlisted_writer(self) -> None:
        """The check can fail: a function that writes to stderr is found."""
        source = "def sneaky(stderr):\n    stderr.write('x')\n"
        self.assertEqual(stderr_writers(source), {"sneaky"}, msg="the scanner finds the writer")


if __name__ == "__main__":
    unittest.main()
