"""The tool schemas take their patterns and bounds from the domain, so the two cannot drift.

Owner decision 2026-11-22 (KICKOFF-13 D1, D2, D3): the run id and name patterns have one owner,
and the memory bound that the MCP schema enforces is also refused by the command line.
"""

import io
import re
import unittest

from agents_dispatcher.application import tool_schemas
from agents_dispatcher.domain import routing, run
from agents_dispatcher.domain.run import MAX_MEMORY_BYTES, RunId, TaskSpec
from agents_dispatcher.interfaces.cli.app import main
from tests.support.cli_book import Book, FixedVersion
from tests.support.cli_failure import failure_of

RUN_ID_SCHEMA_TODAY = "^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
NAME_SCHEMA_TODAY = "^[a-z][a-z0-9-]{0,31}$"
GOOD_ID = "0b8e5f4a-3c1d-4e2a-9b7c-1d2e3f4a5b6c"
RUN_ID_SAMPLES = (
    (GOOD_ID, True),
    (GOOD_ID.replace("-4e2a-", "-3e2a-"), False),
    (GOOD_ID.replace("-9b7c-", "-cb7c-"), False),
    (GOOD_ID.upper(), False),
    (GOOD_ID + "0", False),
    (GOOD_ID[:-1], False),
    ("", False),
)
NAME_SAMPLES = (
    ("codex", True),
    ("a" * 32, True),
    ("a" * 33, False),
    ("Codex", False),
    ("1codex", False),
    ("co_dex", False),
    ("", False),
)


def _accepts_run_id(text: str) -> bool:
    """Say whether the domain takes the text as a run id."""
    try:
        RunId(text)
    except ValueError:
        return False
    return True


def _accepts_name(text: str) -> bool:
    """Say whether the domain takes the text as a task type name or provider id."""
    try:
        routing.validate_name(text, "name")
    except ValueError:
        return False
    return True


class DerivedPatternTests(unittest.TestCase):
    """One owner for each pattern; the published schema text is unchanged."""

    def test_the_published_schema_patterns_are_byte_identical_to_before(self) -> None:
        """Deriving must not change what a client reads."""
        self.assertEqual(tool_schemas.RUN_ID_PATTERN, RUN_ID_SCHEMA_TODAY, msg="run id pattern")
        self.assertEqual(tool_schemas.NAME_PATTERN, NAME_SCHEMA_TODAY, msg="name pattern")

    def test_the_schema_patterns_are_the_domain_patterns_anchored(self) -> None:
        """Change the domain pattern and the schema follows."""
        self.assertEqual(
            tool_schemas.RUN_ID_PATTERN, f"^{run.RUN_ID_PATTERN}$", msg="run id pattern derived"
        )
        self.assertEqual(
            tool_schemas.NAME_PATTERN, f"^{routing.NAME_PATTERN}$", msg="name pattern derived"
        )

    def test_the_domain_and_the_schema_agree_on_every_sample(self) -> None:
        """A value the schema lets through is a value the domain accepts, and the reverse."""
        for text, valid in RUN_ID_SAMPLES:
            with self.subTest(run_id=text):
                self.assertEqual(
                    re.search(tool_schemas.RUN_ID_PATTERN, text) is not None,
                    valid,
                    msg="the schema pattern",
                )
                self.assertEqual(_accepts_run_id(text), valid, msg="the domain")
        for text, valid in NAME_SAMPLES:
            with self.subTest(name=text):
                self.assertEqual(
                    re.search(tool_schemas.NAME_PATTERN, text) is not None,
                    valid,
                    msg="the schema pattern",
                )
                self.assertEqual(_accepts_name(text), valid, msg="the domain")


class MemoryBoundTests(unittest.TestCase):
    """The bound the schema publishes is the bound the domain enforces."""

    def test_the_bound_is_the_largest_signed_64_bit_integer(self) -> None:
        """The run store keeps the limit as a signed 64-bit integer."""
        self.assertEqual(MAX_MEMORY_BYTES, 9223372036854775807, msg="the bound")
        start = next(
            tool for tool in tool_schemas.tool_definitions(20.0) if tool["name"] == "start"
        )
        properties = start["inputSchema"]["properties"]  # type: ignore[index]
        self.assertEqual(
            properties["memory_bytes"]["maximum"], MAX_MEMORY_BYTES, msg="the schema derives it"
        )

    def test_a_task_spec_refuses_a_memory_limit_above_the_bound(self) -> None:
        """The bound itself is allowed, one more is not."""
        self.assertEqual(
            TaskSpec(repo="r", task="t", memory_bytes=MAX_MEMORY_BYTES).memory_bytes,
            MAX_MEMORY_BYTES,
            msg="the bound is accepted",
        )
        with self.assertRaises(ValueError, msg="one above the bound is refused"):
            TaskSpec(repo="r", task="t", memory_bytes=MAX_MEMORY_BYTES + 1)

    def test_the_command_line_refuses_it_before_any_service_is_built(self) -> None:
        """Exit 2 and the failure object, for `start` and for `run`."""
        for command in ("start", "run"):
            with self.subTest(command=command):
                book = Book()
                stdout, stderr = io.StringIO(), io.StringIO()
                argv = [command, "--repo", "r", "--task", "t", "--memory-bytes", str(2**63)]
                if command == "start":
                    argv += ["--provider", "codex"]
                code = main(argv, stdout, stderr, FixedVersion(), book.services, book.scope)
                self.assertEqual(code, 2, msg="invalid input")
                self.assertEqual(
                    failure_of(stdout.getvalue())["code"], "INVALID_ARGUMENT", msg="the code"
                )
                self.assertEqual(book.factory_calls, 0, msg="no services were built")


if __name__ == "__main__":
    unittest.main()
