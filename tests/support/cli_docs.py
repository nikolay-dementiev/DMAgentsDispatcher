"""Rewrite or check the generated CLI documents.

Run from the repository root: `python -m tests.support.cli_docs --write` or `--check`.
"""

import sys
from collections.abc import Sequence
from pathlib import Path

from agents_dispatcher.interfaces.cli.reference import (
    README_END,
    README_START,
    render_readme_section,
    render_reference,
)

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
REFERENCE = ROOT / "docs" / "cli.md"
REWRITE_COMMAND = "python -m tests.support.cli_docs --write"


def extract_block(text: str) -> str:
    """Return what the README holds between the two markers, without the marker lines."""
    start = text.index(README_START) + len(README_START)
    end = text.index(README_END)
    return text[start:end].strip("\n")


def replace_block(text: str, block: str) -> str:
    """Return the README text with the generated block replaced."""
    start = text.index(README_START) + len(README_START)
    end = text.index(README_END)
    return f"{text[:start]}\n{block}\n{text[end:]}"


def main(argv: Sequence[str]) -> int:
    """Write the documents (`--write`) or report whether they are current (`--check`)."""
    if list(argv) not in (["--write"], ["--check"]):
        sys.stderr.write(f"usage: {REWRITE_COMMAND.replace('--write', '--write | --check')}\n")
        return 2
    readme = README.read_text(encoding="utf-8")
    wanted_readme = replace_block(readme, render_readme_section())
    wanted_reference = render_reference()
    current_reference = REFERENCE.read_text(encoding="utf-8") if REFERENCE.exists() else None
    if argv[0] == "--check":
        if readme == wanted_readme and current_reference == wanted_reference:
            return 0
        sys.stderr.write(f"the documents are out of date; run: {REWRITE_COMMAND}\n")
        return 1
    REFERENCE.parent.mkdir(exist_ok=True)
    REFERENCE.write_text(wanted_reference, encoding="utf-8")
    README.write_text(wanted_readme, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
