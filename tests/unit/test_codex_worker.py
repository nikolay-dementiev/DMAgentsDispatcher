"""Tests of the Codex worker adapter: argv, environment, brief handling and reading the events.

The event shapes are those of the scrubbed real captures in `tests/fixtures/codex` (codex-cli
0.160.0, see the README there); the adapter reads nothing those captures do not show.
"""

import json
import tempfile
import unittest
from pathlib import Path

from agents_dispatcher.adapters.system.codex_worker import CodexWorker
from agents_dispatcher.application.ports import (
    EgressEndpoint,
    ExecutionOutcome,
    PreparedWorkspace,
    WorkerInvocation,
)
from agents_dispatcher.domain.routing import TaskMode
from tests.support.fake_executable import FakeExecutable, SourceRef
from tests.support.run_spies import BASE, RUN_ID

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "codex"
KEPT_BYTES = 3000
EGRESS = EgressEndpoint(host="127.0.0.1", port=40123)
INVOCATION = WorkerInvocation(model="gpt-6.1-sol", effort="medium", mode=TaskMode.WRITE)
VERSION_SOURCE = SourceRef(
    kind="captured_output",
    cli_name="codex",
    cli_version="0.160.0",
    excerpt="codex-cli 0.160.0",
)
FROZEN_FLAGS = (
    "exec",
    "--json",
    "--ignore-user-config",
    "--ignore-rules",
    "--ephemeral",
    "--skip-git-repo-check",
    "-s",
    "danger-full-access",
    "-c",
    "apps._default.enabled=false",
    "--disable",
    "apps",
    "--disable",
    "plugins",
    "--disable",
    "remote_plugin",
    "--disable",
    "hooks",
    "--disable",
    "shell_snapshot",
    "--disable",
    "skill_search",
    "--disable",
    "skill_mcp_dependency_install",
    "--disable",
    "tool_suggest",
    "--disable",
    "browser_use",
    "--disable",
    "computer_use",
    "--disable",
    "in_app_browser",
)


def fixture(name: str) -> str:
    """Return the text of one scrubbed real capture."""
    return (FIXTURES / name).read_text(encoding="utf-8")


def stream(*events: dict[str, object]) -> str:
    """Return events as the JSON lines the CLI prints."""
    return "".join(json.dumps(event) + "\n" for event in events)


STARTED: dict[str, object] = {
    "type": "thread.started",
    "thread_id": "00000000-0000-4000-8000-000000000001",
}
DONE: dict[str, object] = {
    "type": "turn.completed",
    "usage": {"input_tokens": 1, "output_tokens": 1},
}


def message(text: str) -> dict[str, object]:
    """Return an `agent_message` completion event."""
    return {
        "type": "item.completed",
        "item": {"id": "item_0", "type": "agent_message", "text": text},
    }


class CodexWorkerCase(unittest.TestCase):
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
        self.worker = CodexWorker(
            executable="/opt/tools/codex",
            home="/home/example",
            user="example",
            kept_bytes=KEPT_BYTES,
        )


class CommandTests(CodexWorkerCase):
    """What is started, with which arguments and environment."""

    def test_the_argument_array_is_the_frozen_one(self) -> None:
        """No shell, the brief on standard input (`-`), the sandbox flag set to the outer one."""
        command = self.worker.command("do the thing", self.workspace, EGRESS, INVOCATION)
        self.assertEqual(
            command.argv,
            (
                "/opt/tools/codex",
                *FROZEN_FLAGS,
                "-m",
                "gpt-6.1-sol",
                "-c",
                'model_reasoning_effort="medium"',
                "-",
            ),
            msg="frozen argv",
        )

    def test_a_read_task_runs_with_the_same_argument_array(self) -> None:
        """The outer profile is the write boundary, not a CLI flag."""
        read = WorkerInvocation(model="gpt-6.1-sol", effort="medium", mode=TaskMode.READ)
        write = self.worker.command("x", self.workspace, EGRESS, INVOCATION).argv
        self.assertEqual(self.worker.command("x", self.workspace, EGRESS, read).argv, write)

    def test_the_bypass_flag_is_never_used(self) -> None:
        """The CLI's own safety bypass stays off; the SDK profile is the boundary."""
        argv = self.worker.command("x", self.workspace, EGRESS, INVOCATION).argv
        self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", argv, msg="no bypass")
        self.assertNotIn("--yolo", argv, msg="no alias of the bypass")

    def test_the_model_and_effort_come_from_the_invocation_of_the_run(self) -> None:
        """Nothing is hard-coded."""
        invocation = WorkerInvocation(model="other-model", effort="xhigh", mode=TaskMode.READ)
        argv = self.worker.command("x", self.workspace, EGRESS, invocation).argv
        self.assertEqual(argv[argv.index("-m") + 1], "other-model", msg="model")
        self.assertIn('model_reasoning_effort="xhigh"', argv, msg="effort")

    def test_the_brief_goes_on_standard_input_never_on_the_command_line(self) -> None:
        """A brief with shell metacharacters stays out of argv."""
        brief = "rm -rf / ; $(whoami) `id`"
        command = self.worker.command(brief, self.workspace, EGRESS, INVOCATION)
        self.assertEqual(command.stdin, brief, msg="on stdin")
        self.assertFalse(any(brief in item for item in command.argv), msg="not in argv")

    def test_the_worker_starts_in_the_worktree(self) -> None:
        """The working directory is the run's worktree."""
        command = self.worker.command("x", self.workspace, EGRESS, INVOCATION)
        self.assertEqual(command.cwd, str(self.sandbox / "worktree"), msg="cwd")

    def test_the_environment_points_the_worker_at_the_sandbox_and_the_proxy(self) -> None:
        """TMPDIR, the real HOME (the CLI's own login) and the proxy in both spellings."""
        environment = self.worker.command("x", self.workspace, EGRESS, INVOCATION).environment
        proxy = "http://127.0.0.1:40123"
        expected = {
            "HOME": "/home/example",
            "USER": "example",
            "LOGNAME": "example",
            "TMPDIR": str(self.sandbox / "tmp"),
            "ZDOTDIR": str(self.sandbox / "zdot"),
            "HTTPS_PROXY": proxy,
            "HTTP_PROXY": proxy,
            "https_proxy": proxy,
            "http_proxy": proxy,
        }
        for key, value in expected.items():
            self.assertEqual(environment.get(key), value, msg=key)
        self.assertTrue((self.sandbox / "tmp").is_dir(), msg="tmp folder")
        self.assertTrue((self.sandbox / "zdot").is_dir(), msg="zdot folder")

    def test_the_environment_is_built_from_scratch(self) -> None:
        """Nothing of the supervisor's shell leaks, and no proxy exemption is passed on."""
        environment = self.worker.command("x", self.workspace, EGRESS, INVOCATION).environment
        for key in ("NO_PROXY", "no_proxy", "OPENAI_API_KEY", "CODEX_HOME", "ANTHROPIC_API_KEY"):
            self.assertNotIn(key, environment, msg=key)

    def test_the_version_is_read_from_a_real_executable(self) -> None:
        """A fake codex prints what 0.160.0 prints; the number is extracted."""
        fake = FakeExecutable("codex", 'echo "codex-cli 0.160.0"\n', VERSION_SOURCE)
        worker = CodexWorker(
            executable=str(fake.write_to(self.root)), home="/h", user="u", kept_bytes=KEPT_BYTES
        )
        self.assertEqual(worker.cli_version(), "0.160.0", msg="version number only")

    def test_an_unreadable_version_is_an_error(self) -> None:
        """Garbage output is not turned into a made-up version."""
        fake = FakeExecutable("codex", "echo nonsense\n", VERSION_SOURCE)
        worker = CodexWorker(
            executable=str(fake.write_to(self.root)), home="/h", user="u", kept_bytes=KEPT_BYTES
        )
        with self.assertRaises(RuntimeError):
            worker.cli_version()


class ParseTests(CodexWorkerCase):
    """Reading the event stream."""

    def parse(self, stdout: str, exit_status: int | None = 0) -> tuple[bool, str]:
        """Parse an outcome and return the verdict and the text."""
        verdict = self.worker.parse(ExecutionOutcome(exit_status=exit_status, stdout=stdout))
        return verdict.succeeded, verdict.result_text

    def test_a_completed_read_turn_succeeds_with_its_final_message(self) -> None:
        """Real capture: thread, turn, one message, completion."""
        self.assertEqual(self.parse(fixture("exec_read_ok.jsonl")), (True, "ok"), msg="read run")

    def test_the_last_message_is_the_report_when_commands_ran(self) -> None:
        """Real capture of a write run: earlier messages are narration, the last is the report."""
        self.assertEqual(
            self.parse(fixture("exec_write_command.jsonl")), (True, "done"), msg="write run"
        )

    def test_no_model_is_reported_because_no_event_names_it(self) -> None:
        """The requested model is labelled as requested by the caller of the adapter."""
        verdict = self.worker.parse(
            ExecutionOutcome(exit_status=0, stdout=fixture("exec_read_ok.jsonl"))
        )
        self.assertIsNone(verdict.model, msg="no invented model")

    def test_a_stream_without_a_completed_turn_is_a_failure(self) -> None:
        """The process ended in the middle of a turn."""
        ok, text = self.parse(stream(STARTED, message("partial")))
        self.assertFalse(ok, msg="no turn.completed")
        self.assertIn("turn", text, msg="says what is missing")

    def test_a_failed_turn_is_a_failure_even_with_exit_status_zero(self) -> None:
        """`turn.failed` defeats success."""
        failed: dict[str, object] = {"type": "turn.failed", "error": {"message": "model refused"}}
        ok, text = self.parse(stream(STARTED, message("x"), failed, DONE))
        self.assertFalse(ok, msg="turn.failed")
        self.assertIn("model refused", text, msg="carries the reason")

    def test_an_error_event_is_a_failure(self) -> None:
        """`error` defeats success."""
        error: dict[str, object] = {"type": "error", "message": "stream disconnected"}
        ok, text = self.parse(stream(STARTED, message("x"), error, DONE))
        self.assertFalse(ok, msg="error event")
        self.assertIn("stream disconnected", text, msg="carries the reason")

    def test_an_empty_final_message_is_a_failure(self) -> None:
        """A report with no text is no report."""
        ok, _ = self.parse(stream(STARTED, message("  \n"), DONE))
        self.assertFalse(ok, msg="blank report")

    def test_a_turn_with_no_agent_message_is_a_failure(self) -> None:
        """Completion without any message."""
        ok, _ = self.parse(stream(STARTED, DONE))
        self.assertFalse(ok, msg="no message")

    def test_a_line_that_is_not_json_is_a_failure_with_the_text_kept(self) -> None:
        """Malformed output is not skipped silently."""
        ok, text = self.parse(fixture("exec_read_ok.jsonl") + "not json at all\n")
        self.assertFalse(ok, msg="malformed line")
        self.assertIn("not json at all", text, msg="the text is kept")

    def test_a_non_zero_exit_status_is_a_failure(self) -> None:
        """A complete stream does not rescue a failing process."""
        ok, text = self.parse(fixture("exec_read_ok.jsonl"), exit_status=2)
        self.assertFalse(ok, msg="exit 2")
        self.assertIn("status 2", text, msg="says the status")

    def test_a_missing_exit_status_is_a_failure(self) -> None:
        """The process did not exit normally."""
        ok, _ = self.parse(fixture("exec_read_ok.jsonl"), exit_status=None)
        self.assertFalse(ok, msg="no status")

    def test_failure_text_is_bounded_by_the_configured_size(self) -> None:
        """A huge unparseable output is cut at `kept_bytes`."""
        ok, text = self.parse("x" * 10_000)
        self.assertFalse(ok, msg="garbage")
        self.assertLess(len(text.encode()), KEPT_BYTES + 200, msg="bounded")

    def test_events_of_unknown_types_are_tolerated(self) -> None:
        """New event kinds in a later CLI do not break a good run."""
        extra: dict[str, object] = {
            "type": "item.completed",
            "item": {"id": "i", "type": "reasoning", "text": "r"},
        }
        ok, text = self.parse(stream(STARTED, extra, message("fine"), DONE))
        self.assertEqual((ok, text), (True, "fine"), msg="unknown item ignored")
