"""Tests of the `mcp serve` command line: it hands over to the server and reads no services."""

import io
import unittest

from agents_dispatcher.interfaces.cli.app import main
from tests.support.cli_book import Book, FixedVersion
from tests.support.cli_exit_check import assert_listed


class Served:
    """A server entry that records its calls and answers with a code."""

    def __init__(self, code: int = 0) -> None:
        """Start with no calls."""
        self.code = code
        self.calls = 0

    def __call__(self) -> int:
        """Count the call."""
        self.calls += 1
        return self.code


def run(argv: list[str], served: Served | None) -> tuple[int, str, str, Book]:
    """Run the command line with the entry and return code, stdout, stderr and the book."""
    book = Book()
    stdout, stderr = io.StringIO(), io.StringIO()
    code = main(argv, stdout, stderr, FixedVersion(), book.services, book.scope, serve_mcp=served)
    assert_listed(argv, code)
    return code, stdout.getvalue(), stderr.getvalue(), book


class McpServeCommandTests(unittest.TestCase):
    """`agentsd mcp serve`."""

    def test_serve_hands_over_and_returns_its_code(self) -> None:
        """The exit code is the server's; no services are built for the command line."""
        served = Served(code=1)
        code, _, _, book = run(["mcp", "serve"], served)
        self.assertEqual(code, 1, msg="the server's code")
        self.assertEqual(served.calls, 1, msg="served once")
        self.assertEqual(book.factory_calls, 0, msg="the use cases were not built by the CLI")

    def test_mcp_without_serve_is_a_usage_error(self) -> None:
        """Exit code 2 and nothing started."""
        served = Served()
        code, _, err, _ = run(["mcp"], served)
        self.assertEqual(code, 2, msg="usage error")
        self.assertEqual(served.calls, 0, msg="not served")
        self.assertIn("serve", err, msg="names the subcommand")

    def test_an_unknown_mcp_argument_is_a_usage_error(self) -> None:
        """No hidden options."""
        served = Served()
        code, _, _, _ = run(["mcp", "serve", "--port", "1"], served)
        self.assertEqual(code, 2, msg="usage error")
        self.assertEqual(served.calls, 0, msg="not served")

    def test_help_lists_the_command(self) -> None:
        """It can be found."""
        _, out, _, _ = run(["--help"], Served())
        self.assertIn("mcp", out, msg="help names mcp")

    def test_an_unwired_server_is_a_failure_not_a_trace(self) -> None:
        """A front end built without a server says so."""
        code, _, err, _ = run(["mcp", "serve"], None)
        self.assertEqual(code, 1, msg="failure")
        self.assertIn("not available", err, msg="says why")

    def test_an_escaped_error_becomes_a_message(self) -> None:
        """The guard of the other commands applies."""

        def broken() -> int:
            message = "boom"
            raise RuntimeError(message)

        book = Book()
        stdout, stderr = io.StringIO(), io.StringIO()
        code = main(
            ["mcp", "serve"],
            stdout,
            stderr,
            FixedVersion(),
            book.services,
            book.scope,
            serve_mcp=broken,
        )
        assert_listed(["mcp", "serve"], code)
        self.assertEqual(code, 1, msg="failure")
        self.assertIn("boom", stderr.getvalue(), msg="named")


if __name__ == "__main__":
    unittest.main()
