"""Tests of the Claude Code worker adapter: argv, environment, brief handling and parsing.

The JSON result shape is the documented `--output-format json` result object of Claude Code
(`type`, `subtype`, `is_error`, `result`). It is checked against a live capture in the manual
run of slice 9; until then the fixtures below are spec excerpts, not captures.
"""

import json
import tempfile
import unittest
from pathlib import Path

from agents_dispatcher.adapters.system.claude_worker import ClaudeCodeWorker
from agents_dispatcher.application.ports import (
    EgressEndpoint,
    ExecutionOutcome,
    PreparedWorkspace,
)
from tests.support.fake_executable import FakeExecutable, SourceRef
from tests.support.run_spies import BASE, RUN_ID

EGRESS = EgressEndpoint(host="127.0.0.1", port=40123)
VERSION_SOURCE = SourceRef(
    kind="captured_output",
    cli_name="claude",
    cli_version="2.1.304",
    excerpt="2.1.304 (Claude Code)",
)
SUCCESS = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "Added the function.",
    "num_turns": 3,
}


class ClaudeWorkerCase(unittest.TestCase):
    """A worker over a temporary sandbox."""

    def setUp(self) -> None:
        """Create the sandbox folders the adapter fills."""
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        self.sandbox = self.root / "sandbox"
        (self.sandbox / "worktree").mkdir(parents=True)
        self.workspace = PreparedWorkspace(
            base_commit=BASE,
            run_id=RUN_ID,
            sandbox_dir=str(self.sandbox),
            worktree_dir=str(self.sandbox / "worktree"),
            branch_name=RUN_ID.branch_name,
        )
        self.worker = ClaudeCodeWorker(
            executable="/opt/tools/claude",
            home="/home/example",
            user="example",
            model="claude-sonnet-5-5",
            effort="medium",
        )


class CommandTests(ClaudeWorkerCase):
    """What is started, with which arguments and environment."""

    def test_the_argument_array_has_the_fixed_isolation_flags(self) -> None:
        """No shell, every flag a separate element, the empty MCP config inside the sandbox."""
        command = self.worker.command("do the thing", self.workspace, EGRESS)
        self.assertEqual(
            command.argv,
            (
                "/opt/tools/claude",
                "-p",
                "--safe-mode",
                "--strict-mcp-config",
                "--setting-sources",
                "",
                "--model",
                "claude-sonnet-5-5",
                "--effort",
                "medium",
                "--mcp-config",
                str(self.sandbox / "mcp-empty.json"),
                "--no-session-persistence",
                "--output-format",
                "json",
                "--permission-mode",
                "bypassPermissions",
            ),
            msg="fixed argv",
        )

    def test_the_brief_goes_on_standard_input_never_on_the_command_line(self) -> None:
        """A task with quotes, newlines and shell syntax stays one block of text."""
        task = 'fix "it"; rm -rf ~\nthen $(whoami)'
        command = self.worker.command(task, self.workspace, EGRESS)
        self.assertEqual(command.stdin, task, msg="the task is the standard input")
        self.assertFalse(any(task in argument for argument in command.argv), msg="not in argv")
        self.assertEqual(command.cwd, self.workspace.worktree_dir, msg="runs in the worktree")

    def test_the_empty_mcp_config_and_private_folders_exist(self) -> None:
        """The adapter prepares what the flags and variables point at."""
        self.worker.command("x", self.workspace, EGRESS)
        config = json.loads((self.sandbox / "mcp-empty.json").read_text(encoding="utf-8"))
        self.assertEqual(config, {"mcpServers": {}}, msg="no MCP servers")
        self.assertTrue((self.sandbox / "tmp").is_dir(), msg="tmp folder")
        self.assertTrue((self.sandbox / "zdot").is_dir(), msg="zdot folder")

    def test_the_environment_points_the_worker_at_the_sandbox_and_the_proxy(self) -> None:
        """TMPDIR variables, the real HOME and user name, and the proxy in both spellings (F6).

        USER is the account name of the keychain item that holds the CLI's login: without it the
        real Claude Code 2.1.304 answers "Not logged in" (found in the first real run).
        """
        environment = self.worker.command("x", self.workspace, EGRESS).environment
        proxy = "http://127.0.0.1:40123"
        expected = {
            "HOME": "/home/example",
            "USER": "example",
            "LOGNAME": "example",
            "TMPDIR": str(self.sandbox / "tmp"),
            "CLAUDE_CODE_TMPDIR": str(self.sandbox / "tmp"),
            "ZDOTDIR": str(self.sandbox / "zdot"),
            "HTTPS_PROXY": proxy,
            "HTTP_PROXY": proxy,
            "https_proxy": proxy,
            "http_proxy": proxy,
        }
        for key, value in expected.items():
            self.assertEqual(environment.get(key), value, msg=key)

    def test_worker_folders_that_exist_with_a_wider_mode_are_narrowed(self) -> None:
        """The worker's tmp and zdot folders are private even if they were there already."""
        for name in ("tmp", "zdot"):
            (self.sandbox / name).mkdir(mode=0o755)
        self.worker.command("x", self.workspace, EGRESS)
        modes = {name: (self.sandbox / name).stat().st_mode & 0o777 for name in ("tmp", "zdot")}
        self.assertEqual(modes, {"tmp": 0o700, "zdot": 0o700}, msg="both narrowed")

    def test_no_proxy_exemptions_are_passed_on(self) -> None:
        """F6: nothing may exempt a host from the proxy."""
        environment = self.worker.command("x", self.workspace, EGRESS).environment
        self.assertNotIn("NO_PROXY", environment, msg="NO_PROXY absent")
        self.assertNotIn("no_proxy", environment, msg="no_proxy absent")

    def test_the_environment_is_built_from_scratch(self) -> None:
        """Nothing from the supervisor's environment leaks in except what is listed."""
        environment = self.worker.command("x", self.workspace, EGRESS).environment
        self.assertNotIn("SSH_AUTH_SOCK", environment, msg="no agent socket")
        self.assertNotIn("ANTHROPIC_API_KEY", environment, msg="no API key from the shell")


class VersionTests(ClaudeWorkerCase):
    """The installed version is read from the CLI itself."""

    def test_the_version_is_read_from_a_real_executable(self) -> None:
        """A fake claude prints what 2.1.304 prints; the number is extracted."""
        fake = FakeExecutable("claude", 'echo "2.1.304 (Claude Code)"\n', VERSION_SOURCE)
        path = fake.write_to(self.root)
        worker = ClaudeCodeWorker(
            executable=str(path), home="/home/example", user="example", model="m", effort="low"
        )
        self.assertEqual(worker.cli_version(), "2.1.304", msg="version number only")

    def test_an_unreadable_version_is_an_error(self) -> None:
        """Garbage output is not turned into a made-up version."""
        fake = FakeExecutable("claude", "echo nonsense\n", VERSION_SOURCE)
        worker = ClaudeCodeWorker(
            executable=str(fake.write_to(self.root)), home="/h", user="u", model="m", effort="low"
        )
        with self.assertRaises(RuntimeError):
            worker.cli_version()


class ParseTests(ClaudeWorkerCase):
    """Reading what the CLI printed."""

    def parse(self, stdout: str, exit_status: int | None = 0) -> tuple[bool, str]:
        """Parse an outcome and return the verdict and the text."""
        verdict = self.worker.parse(ExecutionOutcome(exit_status=exit_status, stdout=stdout))
        return verdict.succeeded, verdict.result_text

    def test_a_success_result_is_a_success_with_its_text(self) -> None:
        """Exit 0 and a non-error result."""
        self.assertEqual(
            self.parse(json.dumps(SUCCESS)), (True, "Added the function."), msg="success"
        )

    def model_of(self, body: dict[str, object]) -> str | None:
        """Parse a result body and return the model the adapter read from it."""
        outcome = ExecutionOutcome(exit_status=0, stdout=json.dumps(body))
        return self.worker.parse(outcome).model

    def test_the_model_is_the_key_of_model_usage_in_the_result(self) -> None:
        """`modelUsage` is keyed by the model name (observed in a real result of 2.1.304)."""
        usage = {"claude-sonnet-5-5": {"inputTokens": 2, "outputTokens": 6}}
        self.assertEqual(
            self.model_of({**SUCCESS, "modelUsage": usage}), "claude-sonnet-5-5", msg="one model"
        )

    def test_the_model_with_the_most_tokens_is_named_when_several_were_used(self) -> None:
        """A helper model that answered a few tokens does not rename the run."""
        usage = {
            "claude-haiku-5-5": {"inputTokens": 10, "outputTokens": 2},
            "claude-sonnet-5-5": {"inputTokens": 900, "outputTokens": 400},
        }
        self.assertEqual(
            self.model_of({**SUCCESS, "modelUsage": usage}), "claude-sonnet-5-5", msg="main model"
        )

    def test_a_result_without_usable_model_usage_names_no_model(self) -> None:
        """Nothing is guessed: missing, empty and malformed `modelUsage` all give None."""
        for usage in (None, {}, [], "x", {"m": "not a dict"}):
            body = SUCCESS if usage is None else {**SUCCESS, "modelUsage": usage}
            self.assertIsNone(self.model_of(body), msg=f"modelUsage={usage!r}")

    def test_an_error_result_is_a_failure_even_with_exit_status_zero(self) -> None:
        """`is_error` decides, not the exit status."""
        body = {**SUCCESS, "subtype": "error_max_turns", "is_error": True, "result": "Out of turns"}
        succeeded, text = self.parse(json.dumps(body))
        self.assertFalse(succeeded, msg="an error result is not a success")
        self.assertIn("Out of turns", text, msg="the reason is kept")

    def test_a_non_zero_exit_status_is_a_failure(self) -> None:
        """Even with a success-looking body."""
        self.assertFalse(self.parse(json.dumps(SUCCESS), exit_status=1)[0], msg="exit 1")

    def test_a_missing_exit_status_is_a_failure(self) -> None:
        """A worker that was stopped has no status."""
        self.assertFalse(self.parse(json.dumps(SUCCESS), exit_status=None)[0], msg="no status")

    def test_output_that_is_not_json_is_a_failure_with_the_text_kept(self) -> None:
        """Nothing is guessed from unparseable output."""
        succeeded, text = self.parse("segmentation fault")
        self.assertFalse(succeeded, msg="not json")
        self.assertIn("segmentation fault", text, msg="raw output kept for the report")

    def test_json_without_the_expected_fields_is_a_failure(self) -> None:
        """A different shape is not silently accepted."""
        self.assertFalse(self.parse(json.dumps({"hello": "world"}))[0], msg="wrong shape")


if __name__ == "__main__":
    unittest.main()
