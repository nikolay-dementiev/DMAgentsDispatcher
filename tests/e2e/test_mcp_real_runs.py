"""End-to-end tests of `agentsd mcp serve` with real runs.

The shipped composition root serves a temporary account; the worker is a fake claude, the
supervisor is a real detached process and the command line is the second front end.
"""

import json
import tempfile
import time
import unittest

from tests.e2e import test_walking_skeleton as skeleton
from tests.e2e.test_background_runs import BackgroundCase
from tests.process.test_mcp_server_process import Server, call

SLOW = "sleep 3\nprintf '%s\\n' x > done.txt\n" + skeleton.success_json("slow and done")
EXIT_COMPLETED = 0
BOUND_MARGIN_S = 5.0


class RealServerCase(BackgroundCase):
    """A server process over a temporary account, and a way to follow its runs from the CLI."""

    def setUp(self) -> None:
        """Keep the user settings where the shipped composition root reads them."""
        super().setUp()
        self.settings_file = self.home / ".config" / "agents-dispatcher" / "settings.toml"
        self.settings_file.parent.mkdir(parents=True)
        self.settings()
        self.logs = tempfile.TemporaryDirectory()
        self.addCleanup(self.logs.cleanup)

    def serve(self) -> Server:
        """Start `agentsd mcp serve` against the temporary account and open the session."""
        server = Server(
            self.logs.name,
            module="tests.support.mcp_real_child",
            extra_env={"MCP_REAL_HOME": str(self.home)},
        )
        self.addCleanup(server.close)
        server.open()
        return server

    def route(self, server: Server) -> str:
        """Route the test task through the server and return the run id."""
        server.send(
            call(
                1,
                "route",
                {"repo": str(self.repo), "task": skeleton.TASK, "task_type": "implement"},
            )
        )
        answer = server.receive()
        self.assertEqual(answer["id"], 1, msg="the route is answered")
        result = answer["result"]
        assert isinstance(result, dict)  # noqa: S101 - narrows the type
        self.assertFalse(result["isError"], msg=f"the route is accepted: {result}")
        structured = result["structuredContent"]
        assert isinstance(structured, dict)  # noqa: S101 - narrows the type
        return str(structured["run_id"])

    def assert_run_completes_and_is_reportable(self, run_id: str) -> None:
        """Follow the run from the command line to its end and read its report."""
        waited = self.cli("wait", run_id)
        self.assertEqual(waited.code, EXIT_COMPLETED, msg=f"the run completed: {waited.stderr}")
        report = self.cli("report", run_id)
        self.assertEqual(report.code, 0, msg=f"the report is printed: {report.stderr}")
        self.assertEqual(
            json.loads(report.stdout)["terminal_reason"], "COMPLETED", msg="and it completed"
        )
        # The supervisor may still be closing its files; the account folder is removed after this.
        self.until(
            lambda: not json.loads(self.cli("status", run_id).stdout)["owner_alive"],
            "the supervisor to exit",
        )


class RealRunTests(RealServerCase):
    """What the KickOff promises about a run that outlives the call or the connection."""

    def test_cancelling_a_wait_ends_the_wait_and_the_run_continues_to_its_end(self) -> None:
        """The client gives up waiting; the run does not."""
        self.install(SLOW)
        server = self.serve()
        run_id = self.route(server)
        server.send(call(2, "wait", {"run_id": run_id, "timeout_s": 20}))
        time.sleep(0.5)
        server.send(
            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 2}}
        )
        server.send({"jsonrpc": "2.0", "id": 3, "method": "ping"})
        self.assertEqual(server.receive()["id"], 3, msg="the server went on and answered the ping")
        self.assertEqual(
            [json.loads(raw).get("id") for raw in server.raw if b'"id":2' in raw],
            [],
            msg="the cancelled wait is never answered",
        )
        self.assert_run_completes_and_is_reportable(run_id)

    def test_end_of_input_with_an_active_run_exits_within_the_bound_and_the_run_completes(
        self,
    ) -> None:
        """The client goes away mid-run: the server leaves in time, the run is reportable."""
        self.install(SLOW)
        server = self.serve()
        run_id = self.route(server)
        self.running(run_id)
        bound_s = self.settings().mcp.shutdown_total_s + BOUND_MARGIN_S
        began = time.monotonic()
        server.close_input()
        code = server.finish(bound_s)
        self.assertEqual(code, 0, msg="a clean exit")
        self.assertLess(time.monotonic() - began, bound_s, msg="within the shutdown bound")
        self.assert_run_completes_and_is_reportable(run_id)


if __name__ == "__main__":
    unittest.main()
