"""The tools do not depend on the own transport: another McpTransport serves them unchanged."""

import ast
import json
import threading
import unittest
from pathlib import Path

from agents_dispatcher.application.tool_endpoint import ServicesToolEndpoint
from agents_dispatcher.interfaces.mcp.ports import ToolEndpoint
from tests.support.cli_book import Book
from tests.support.run_spies import RUN_ID

ROOT = Path(__file__).resolve().parents[2] / "src" / "agents_dispatcher"
OWN_TRANSPORT_MODULES = frozenset(
    {"framing", "protocol", "session", "outbox", "server", "stream_transport"}
)


class FakeTransport:
    """A transport that is not ours: it lists the tools and makes one call, in memory."""

    def __init__(self) -> None:
        """Start with nothing served."""
        self.listed: list[str] = []
        self.answers: list[dict[str, object]] = []

    def serve(self, endpoint: ToolEndpoint, shutdown: threading.Event) -> int:
        """List the tools, call each run-addressed one once, and stop."""
        self.listed = [str(tool["name"]) for tool in endpoint.tools()]
        for name in ("status", "report"):
            outcome = endpoint.call(name, {"run_id": RUN_ID.value}, shutdown.is_set)
            self.answers.append(outcome.payload)
        return 0


class OpenClosedTests(unittest.TestCase):
    """Adding a transport adds code; it changes none of the tools."""

    def test_a_second_transport_serves_all_six_tools_through_the_port(self) -> None:
        """The endpoint is all a transport needs."""
        transport = FakeTransport()
        endpoint = ServicesToolEndpoint(
            services=Book().services(), wait_max_s=20.0, parse_json=json.loads
        )
        code = transport.serve(endpoint, threading.Event())
        self.assertEqual(code, 0, msg="served")
        self.assertEqual(
            transport.listed,
            ["route", "start", "status", "wait", "stop", "report"],
            msg="the six tools",
        )
        self.assertEqual(len(transport.answers), 2, msg="two calls answered")

    def test_no_tool_module_imports_the_own_transport(self) -> None:
        """The application layer and the adapters name no module of the own transport."""
        offenders = []
        for path in sorted((ROOT / "application").glob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                names = _imported_modules(node)
                if any(_is_own_transport(name) for name in names):
                    offenders.append(path.name)
        self.assertEqual(offenders, [], msg="application modules importing interfaces.mcp")


def _imported_modules(node: ast.AST) -> list[str]:
    """Return the dotted module names an import node names."""
    if isinstance(node, ast.Import):
        return [alias.name for alias in node.names]
    if isinstance(node, ast.ImportFrom) and node.module:
        return [node.module]
    return []


def _is_own_transport(module: str) -> bool:
    """Return whether a module is one of the own transport's modules."""
    parts = module.split(".")
    return "mcp" in parts and bool(OWN_TRANSPORT_MODULES & set(parts) or parts[-1] == "mcp")


if __name__ == "__main__":
    unittest.main()
