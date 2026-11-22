"""The README command section and docs/cli.md are renderings of the catalogue and stay in step."""

import json
import re
import shlex
import unittest

from agents_dispatcher.interfaces.cli.reference import render_readme_section, render_reference

from agents_dispatcher.domain.run import TerminalReason
from agents_dispatcher.interfaces.cli.app import build_parser
from agents_dispatcher.interfaces.cli.catalogue import CATALOGUE
from tests.support.cli_book import make_report
from tests.support.cli_docs import (
    README,
    REFERENCE,
    REWRITE_COMMAND,
    extract_block,
)
from tests.unit.test_cli_help import COMMANDS, label

FENCE = re.compile(r"```bash\n(agentsd [^\n]*)\n```")


def readme_block() -> str:
    """Return the generated block of the README."""
    return extract_block(README.read_text(encoding="utf-8"))


def examples_of_readme() -> list[str]:
    """Return the example column of the README's command table."""
    rows = [line for line in readme_block().splitlines() if line.startswith("| `agentsd ")]
    return [row.split("|")[3].strip().strip("`") for row in rows]


def examples_of_reference() -> list[str]:
    """Return every fenced example of the reference."""
    return FENCE.findall(REFERENCE.read_text(encoding="utf-8"))


class SyncTests(unittest.TestCase):
    """A hand edit, or a catalogue change without a rewrite, fails."""

    def test_the_reference_file_equals_its_rendering(self) -> None:
        """`docs/cli.md` is generated; the failure names the one command that rewrites it."""
        self.assertEqual(
            REFERENCE.read_text(encoding="utf-8"),
            render_reference(),
            msg=f"docs/cli.md is out of date; run: {REWRITE_COMMAND}",
        )

    def test_the_readme_block_equals_its_rendering(self) -> None:
        """The README holds one generated block between two markers."""
        self.assertEqual(
            readme_block(),
            render_readme_section(),
            msg=f"the README command section is out of date; run: {REWRITE_COMMAND}",
        )


class CoverageTests(unittest.TestCase):
    """Every command is in both documents."""

    def test_every_command_appears_in_the_readme_section_and_in_the_reference(self) -> None:
        """A command without its entry in either place fails."""
        block = readme_block()
        reference = REFERENCE.read_text(encoding="utf-8")
        for path in COMMANDS:
            with self.subTest(command=label(path)):
                self.assertIn(f"| `agentsd {label(path)}` |", block, msg="a row in the README")
                self.assertIn(f"### `agentsd {label(path)}`", reference, msg="a part of the file")

    def test_the_section_says_the_output_is_json_and_links_the_reference(self) -> None:
        """No `--json` flag exists; the reader is told why none is needed."""
        block = readme_block()
        self.assertIn("JSON", block, msg="the output format is named")
        self.assertIn("docs/cli.md", block, msg="the full reference is linked")


class ExampleTests(unittest.TestCase):
    """Every example is accepted by the real parser."""

    def test_every_example_in_both_documents_parses(self) -> None:
        """Parsed only: no service is built, nothing runs."""
        found = [*examples_of_readme(), *examples_of_reference()]
        self.assertGreaterEqual(len(found), 2 * len(COMMANDS), msg="an example per command")
        for example in found:
            with self.subTest(example=example):
                words = shlex.split(example)
                self.assertEqual(words[0], "agentsd", msg="a command line of the program")
                build_parser().parse_args(words[1:])


class SampleTests(unittest.TestCase):
    """The JSON samples of the reference are the shape the commands print."""

    def test_every_sample_is_json_and_has_exactly_the_named_members(self) -> None:
        """A renamed member fails the docs test too."""
        for path in COMMANDS:
            entry = CATALOGUE[path]
            if entry.members is None:
                continue
            with self.subTest(command=label(path)):
                self.assertIsNotNone(entry.sample, msg="a command with members has a sample")
                sample = json.loads(entry.sample or "{}")
                self.assertEqual(list(sample), list(entry.members), msg="same members, same order")

    def test_the_report_members_are_the_ones_a_stored_report_has(self) -> None:
        """`run` and `report` print the stored report."""
        expected = tuple(make_report(TerminalReason.COMPLETED).as_dict())
        for path in (("run",), ("report",)):
            with self.subTest(command=label(path)):
                self.assertEqual(CATALOGUE[path].members, expected, msg="the report's members")


if __name__ == "__main__":
    unittest.main()
