"""The command line and the MCP tool endpoint name the same failure by the same code.

Owner decision 2026-11-22: a failure prints the MCP failure object. The failures a use case raises
carry the same code and message in both entry points; the refusals of malformed arguments carry
the same code and each row says whether the message differs.
"""

import io
import unittest

from agents_dispatcher.application.tool_schemas import INVALID_ARGUMENT
from agents_dispatcher.interfaces.cli.app import main
from tests.support.cli_book import Book, FakeStatus, FakeStop, FixedVersion
from tests.support.cli_failure import failure_of
from tests.support.run_spies import RUN_ID
from tests.unit.test_cli_failure_object import RAISED
from tests.unit.test_tool_endpoint import make_endpoint, never

ID = RUN_ID.value


def cli_failure(argv: list[str], book: Book) -> tuple[int, dict[str, str]]:
    """Run the command and return its exit code and the failure object it printed."""
    stdout, stderr = io.StringIO(), io.StringIO()
    code = main(argv, stdout, stderr, FixedVersion(), book.services, book.scope)
    return code, failure_of(stdout.getvalue())


def tool_failure(name: str, arguments: dict[str, object], book: Book) -> dict[str, object]:
    """Call the tool and return the `error` object of its payload."""
    outcome = make_endpoint(book).call(name, arguments, never)
    error = outcome.payload["error"]
    assert isinstance(error, dict)  # noqa: S101 - narrows the type
    return error


class RaisedParityTests(unittest.TestCase):
    """A failure a use case raises is the same object in both entry points."""

    def test_every_raised_failure_has_the_same_code_and_message_in_both(self) -> None:
        """Both read the one failure table, so neither can drift from the other."""
        for error in RAISED:
            with self.subTest(error=type(error).__name__):
                _, printed = cli_failure(["status", ID], Book(status=FakeStatus(error=error)))
                carried = tool_failure(
                    "status", {"run_id": ID}, Book(status=FakeStatus(error=error))
                )
                self.assertEqual(printed, carried, msg="the command line prints what MCP carries")


class ArgumentParityTests(unittest.TestCase):
    """Malformed arguments: one code, the message is worded by each entry point."""

    def test_a_bad_run_id_has_one_code_and_two_wordings(self) -> None:
        """The command line names the argument; the tool names the schema pattern."""
        code, printed = cli_failure(["status", "zzz"], Book())
        carried = tool_failure("status", {"run_id": "zzz"}, Book())
        self.assertEqual(code, 2, msg="invalid input")
        self.assertEqual(printed["code"], INVALID_ARGUMENT, msg="the command line's code")
        self.assertEqual(carried["code"], INVALID_ARGUMENT, msg="the tool's code")
        self.assertNotEqual(printed["message"], carried["message"], msg="the wording differs")

    def test_a_zero_timeout_has_one_code_and_two_wordings(self) -> None:
        """The command line names the option; the tool names the field."""
        code, printed = cli_failure(["wait", ID, "--timeout-s", "0"], Book())
        carried = tool_failure("wait", {"run_id": ID, "timeout_s": 0}, Book())
        self.assertEqual(code, 2, msg="invalid input")
        self.assertEqual(printed["code"], INVALID_ARGUMENT, msg="the command line's code")
        self.assertEqual(carried["code"], INVALID_ARGUMENT, msg="the tool's code")
        self.assertNotEqual(printed["message"], carried["message"], msg="the wording differs")

    def test_a_stop_reason_the_use_case_refuses_has_one_code_and_one_message(self) -> None:
        """A `ValueError` of the use case is worded once, so both print the same message."""
        error = ValueError("reason must not be blank")
        code, printed = cli_failure(["stop", ID, "--reason", "x"], Book(stop=FakeStop(error=error)))
        carried = tool_failure(
            "stop", {"run_id": ID, "reason": "x"}, Book(stop=FakeStop(error=error))
        )
        self.assertEqual(code, 2, msg="invalid input")
        self.assertEqual(printed, carried, msg="the same code and the same message")


if __name__ == "__main__":
    unittest.main()
