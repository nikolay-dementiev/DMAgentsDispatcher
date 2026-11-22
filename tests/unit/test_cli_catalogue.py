"""The command catalogue: what each command says about itself, and that it stays true."""

import argparse
import json
import unittest

from agents_dispatcher.application.quarantine_resolution import Resolution, ResolutionOutcome
from agents_dispatcher.interfaces.cli.app import build_parser, command_parsers
from agents_dispatcher.interfaces.cli.catalogue import CATALOGUE, CommandEntry
from agents_dispatcher.interfaces.cli.exit_codes import EXIT_CODES
from tests.support.cli_book import Book, invoke
from tests.support.run_spies import RUN_ID
from tests.unit.test_cli_help import COMMANDS, label

MEANING = {row.code: row.meaning for row in EXIT_CODES}
COMMAND_LINE = "agentsd <command> --help"


def help_of(path: tuple[str, ...]) -> str:
    """Return what `agentsd <path> --help` prints."""
    return invoke([*path, "--help"], Book()).stdout


def code_line(code: int) -> str:
    """Return the line the help shows for one exit code."""
    return f"{code:>3}  {MEANING[code]}"


class CoverageTests(unittest.TestCase):
    """The catalogue, the parsers and the exit-code table describe the same set."""

    def test_the_catalogue_has_an_entry_for_every_parser_and_nothing_else(self) -> None:
        """The root and the ten commands; a parser without an entry fails here."""
        parsers = command_parsers(build_parser())
        self.assertEqual(set(CATALOGUE), set(parsers), msg="entries equal parsers")
        self.assertEqual(set(CATALOGUE), {(), *COMMANDS}, msg="the root and the ten commands")

    def test_the_union_of_the_exit_code_lists_is_the_table(self) -> None:
        """A code no command lists, or a listed code without a row, fails."""
        listed = {code for entry in CATALOGUE.values() for code in entry.exit_codes}
        self.assertEqual(listed - {0, 1, 2, 3}, {c for c in MEANING if c > 3}, msg="high codes")
        self.assertTrue(listed <= set(MEANING), msg="every listed code has a row")

    def test_every_option_of_every_parser_has_help_text(self) -> None:
        """A new option without a help string cannot reach a release."""
        for path, parser in command_parsers(build_parser()).items():
            for action in parser._actions:  # noqa: SLF001 - the only way to enumerate options
                if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
                    continue
                with self.subTest(command=label(path), option=action.dest):
                    self.assertTrue(action.help, msg="the option explains itself")


class HelpContentTests(unittest.TestCase):
    """What each command's help carries."""

    def test_each_help_shows_its_description_output_exit_codes_and_example(self) -> None:
        """Every catalogue field reaches the screen; the exit codes carry the table's meaning."""
        for path in COMMANDS:
            entry: CommandEntry = CATALOGUE[path]
            text = help_of(path)
            with self.subTest(command=label(path)):
                self.assertIn(entry.description, text, msg="the description")
                self.assertIn(f"Output: {entry.output}", text, msg="what is printed")
                self.assertIn(entry.example, text, msg="the example")
                for code in entry.exit_codes:
                    self.assertIn(code_line(code), text, msg=f"exit code {code}")

    def test_each_help_names_every_option_of_its_parser(self) -> None:
        """Read from the parser, so a new option shows up here without a test edit."""
        for path, parser in command_parsers(build_parser()).items():
            text = help_of(path)
            for action in parser._actions:  # noqa: SLF001 - enumerating the options
                for option in action.option_strings:
                    with self.subTest(command=label(path), option=option):
                        self.assertIn(option, text, msg="the option is listed")

    def test_the_root_help_lists_the_whole_table_and_how_to_get_a_command_help(self) -> None:
        """The agent that reads only `agentsd --help` learns every exit code."""
        text = invoke(["--help"], Book()).stdout
        self.assertIn(CATALOGUE[()].description, text, msg="the product line")
        self.assertIn(COMMAND_LINE, text, msg="how to get a command's help")
        for code in MEANING:
            with self.subTest(code=code):
                self.assertIn(code_line(code), text, msg="the row of the table")


class MembersTests(unittest.TestCase):
    """The members the catalogue names are the members the command prints."""

    def printed(self, argv: list[str], book: Book | None = None) -> dict[str, object]:
        """Run a command against fakes and parse the one JSON object it prints."""
        outcome = invoke(argv, book or Book())
        loaded = json.loads(outcome.stdout)
        assert isinstance(loaded, dict)  # noqa: S101 - a test of the printed shape
        return loaded

    def test_the_named_members_equal_the_printed_ones(self) -> None:
        """A renamed or added member without a catalogue edit fails."""
        run_id = RUN_ID.value
        book = Book()
        book.quarantine.result = Resolution(RUN_ID, ResolutionOutcome.SETTLED)
        printed = {
            ("start",): self.printed(["start", "--repo", "r", "--task", "t"]),
            ("status",): self.printed(["status", run_id]),
            ("wait",): self.printed(["wait", run_id]),
            ("stop",): self.printed(["stop", run_id, "--reason", "x"]),
            ("quarantine", "resolve"): self.printed(["quarantine", "resolve", run_id], book),
        }
        for path, value in printed.items():
            members = CATALOGUE[path].members
            with self.subTest(command=label(path)):
                self.assertIsNotNone(members, msg="a fixed-shape object names its members")
                self.assertEqual(set(value), set(members or ()), msg="the same members")

    def test_the_commands_without_a_fixed_object_name_no_members(self) -> None:
        """`report` and `run` print the stored report; the groups and `mcp serve` print none."""
        for path in (("run",), ("report",), ("quarantine",), ("mcp",), ("mcp", "serve"), ()):
            with self.subTest(command=label(path)):
                self.assertIsNone(CATALOGUE[path].members, msg="no fixed object")


if __name__ == "__main__":
    unittest.main()
