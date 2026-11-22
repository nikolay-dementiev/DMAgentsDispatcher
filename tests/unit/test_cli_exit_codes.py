"""The exit-code table: one source for the code, the help and the README."""

import unittest
from pathlib import Path

from agents_dispatcher.interfaces.cli.exit_codes import (
    EXIT_AUTHORITY_VIOLATION,
    EXIT_BY_KIND,
    EXIT_BY_REASON,
    EXIT_CODES,
    EXIT_FAILURE,
    EXIT_INVALID_INPUT,
    EXIT_OK,
    EXIT_PENDING,
    EXIT_REJECTED,
    EXIT_REPORT_UNAVAILABLE,
    EXIT_UNKNOWN_RUN,
    render_markdown,
)

README = Path(__file__).resolve().parents[2] / "README.md"
HEADER = "| Exit code | Meaning |"


def readme_block() -> str:
    """Return the README's exit-code table: its header line and every row under it."""
    lines = README.read_text(encoding="utf-8").splitlines()
    start = lines.index(HEADER)
    end = start
    while end < len(lines) and lines[end].startswith("|"):
        end += 1
    return "\n".join(lines[start:end])


class TableShapeTests(unittest.TestCase):
    """The table itself is well formed."""

    def test_codes_are_unique_ascending_and_every_row_is_filled_in(self) -> None:
        """A row without a name or a meaning, or a repeated code, is a defect."""
        codes = [row.code for row in EXIT_CODES]
        self.assertEqual(codes, sorted(set(codes)), msg="unique and in ascending order")
        for row in EXIT_CODES:
            self.assertTrue(row.name.strip(), msg=f"code {row.code} has a name")
            self.assertTrue(row.meaning.strip(), msg=f"code {row.code} has a meaning")

    def test_the_table_holds_exactly_the_codes_the_commands_can_return(self) -> None:
        """A code the program returns without a row, or a row nobody returns, fails."""
        returned = {EXIT_OK, EXIT_FAILURE, EXIT_INVALID_INPUT, EXIT_PENDING}
        returned |= set(EXIT_BY_REASON.values()) | set(EXIT_BY_KIND.values())
        returned |= {
            EXIT_REJECTED,
            EXIT_UNKNOWN_RUN,
            EXIT_AUTHORITY_VIOLATION,
            EXIT_REPORT_UNAVAILABLE,
        }
        self.assertEqual({row.code for row in EXIT_CODES}, returned, msg="table equals the code")


class ReadmeTests(unittest.TestCase):
    """The README states the table, row for row."""

    def test_the_readme_block_equals_the_rendered_table(self) -> None:
        """A row added, removed or reworded on either side shows here."""
        self.assertEqual(readme_block(), render_markdown(), msg="README block equals the render")

    def test_the_row_for_one_names_the_quarantine_with_warnings_case(self) -> None:
        """`quarantine resolve` exits 1 when the run settled but a warning followed."""
        row = next(item for item in EXIT_CODES if item.code == EXIT_FAILURE)
        self.assertIn("quarantine resolve", row.meaning, msg="the case is in the meaning")
        self.assertIn("warning", row.meaning, msg="and says why it exits 1")


if __name__ == "__main__":
    unittest.main()
