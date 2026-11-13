"""Unit tests of `agentsd run`: arguments, report on stdout, messages on stderr, exit codes."""

import io
import json
import unittest
from collections.abc import Sequence
from dataclasses import dataclass, field

from agents_dispatcher.application.errors import (
    RunStoreError,
    WorkspaceError,
    WorkspaceNotGitError,
)
from agents_dispatcher.application.settings import SettingsError
from agents_dispatcher.domain.run import (
    LimitKind,
    Limits,
    RunReport,
    RunState,
    TaskSpec,
    TerminalReason,
)
from agents_dispatcher.interfaces.cli.app import main
from tests.support.run_spies import BASE, RUN_ID


def make_report(reason: TerminalReason, *, limit: LimitKind | None = None) -> RunReport:
    """Build a terminal report with the given reason."""
    return RunReport(
        run_id=RUN_ID,
        state=RunState.TERMINAL,
        terminal_reason=reason,
        repo="/work/example",
        base_commit=BASE,
        branch=RUN_ID.branch_name if reason is TerminalReason.COMPLETED else None,
        commits_on_branch=1 if reason is TerminalReason.COMPLETED else 0,
        worker_cli_version="2.1.304",
        worker_exit_status=0,
        duration_s=1.5,
        limits_configured=Limits(hard_s=60.0, memory_bytes=1024),
        limit_hit=limit,
        survivors=(),
        result_text="the worker said this",
        notes=("uncommitted changes in the repository are not included",),
    )


@dataclass
class SpyService:
    """A run service that records the specification and returns or raises what it is told."""

    report: RunReport | None = None
    error: Exception | None = None
    specs: list[TaskSpec] = field(default_factory=list)

    def run(self, spec: TaskSpec) -> RunReport:
        """Record the specification, then raise or return."""
        self.specs.append(spec)
        if self.error is not None:
            raise self.error
        if self.report is None:
            message = "the test gave the spy neither a report nor an error"
            raise AssertionError(message)
        return self.report


class FixedVersion:
    """A version source for tests that never read the version."""

    def installed_version(self) -> str | None:
        """Return a fixed version."""
        return "9.9.9-test"


@dataclass(frozen=True)
class Outcome:
    """Everything one command run produced."""

    code: int
    stdout: str
    stderr: str
    factory_calls: int


def run(argv: Sequence[str], service: SpyService | Exception) -> Outcome:
    """Run the command; a factory that raises stands for settings that cannot be loaded."""
    calls = []

    def factory() -> SpyService:
        calls.append(1)
        if isinstance(service, Exception):
            raise service
        return service

    stdout, stderr = io.StringIO(), io.StringIO()
    code = main(argv, stdout, stderr, FixedVersion(), factory)
    return Outcome(code, stdout.getvalue(), stderr.getvalue(), len(calls))


ARGS = ["run", "--repo", "/work/example", "--task", "add a function"]


class RunSuccessTests(unittest.TestCase):
    """A completed run."""

    def test_the_report_is_one_json_object_on_stdout(self) -> None:
        """The printed object is the contract form of the report; stderr stays empty."""
        report = make_report(TerminalReason.COMPLETED)
        outcome = run(ARGS, SpyService(report=report))
        self.assertEqual(outcome.code, 0, msg="a completed run exits 0")
        self.assertEqual(json.loads(outcome.stdout), report.as_dict(), msg="the report")
        self.assertEqual(outcome.stderr, "", msg="no message for a clean run")

    def test_the_task_and_repository_reach_the_use_case(self) -> None:
        """No overrides unless the caller gives them."""
        service = SpyService(report=make_report(TerminalReason.COMPLETED))
        run(ARGS, service)
        self.assertEqual(
            service.specs, [TaskSpec(repo="/work/example", task="add a function")], msg="spec"
        )

    def test_limit_overrides_are_passed_on(self) -> None:
        """`--hard-s` and `--memory-bytes` become per-run overrides."""
        service = SpyService(report=make_report(TerminalReason.COMPLETED))
        run([*ARGS, "--hard-s", "90.5", "--memory-bytes", "4096"], service)
        self.assertEqual(service.specs[0].hard_s, 90.5, msg="hard_s override")
        self.assertEqual(service.specs[0].memory_bytes, 4096, msg="memory override")


class ExitCodeTests(unittest.TestCase):
    """The exit code follows the terminal reason (architecture mapping, Q2)."""

    def test_each_terminal_reason_has_its_own_code_and_a_message(self) -> None:
        """A run that did not complete says why on stderr and never exits 0."""
        expected = {
            TerminalReason.FAILED: 10,
            TerminalReason.STOPPED: 13,
            TerminalReason.ORPHANED: 14,
            TerminalReason.RESOURCE_LIMIT: 16,
            TerminalReason.LAUNCH_LOST: 17,
        }
        for reason, code in expected.items():
            with self.subTest(reason=reason.name):
                outcome = run(ARGS, SpyService(report=make_report(reason)))
                self.assertEqual(outcome.code, code, msg=f"exit code of {reason.name}")
                self.assertIn(reason.name, outcome.stderr, msg="the reason is named")
                self.assertIn(RUN_ID.value, outcome.stderr, msg="the run id is named")
                self.assertEqual(
                    json.loads(outcome.stdout)["terminal_reason"],
                    reason.name,
                    msg="the report is still printed",
                )

    def test_a_resource_limit_names_the_limit_in_the_message(self) -> None:
        """The settings key of the limit that was hit is shown."""
        report = make_report(TerminalReason.RESOURCE_LIMIT, limit=LimitKind.MEMORY)
        outcome = run(ARGS, SpyService(report=report))
        self.assertIn("resources.run.memory_bytes", outcome.stderr, msg="limit key")


class UsageErrorTests(unittest.TestCase):
    """Input the command refuses before or at the start of a run."""

    def test_missing_task_or_repository_is_invalid_input(self) -> None:
        """Both flags are required; nothing is started."""
        for argv in (["run", "--repo", "/x"], ["run", "--task", "t"], ["run"]):
            with self.subTest(argv=argv):
                outcome = run(argv, SpyService(report=make_report(TerminalReason.COMPLETED)))
                self.assertEqual(outcome.code, 2, msg="invalid input exits 2")
                self.assertEqual(outcome.stdout, "", msg="no report")
                self.assertEqual(outcome.factory_calls, 0, msg="nothing was built")

    def test_a_non_numeric_limit_is_invalid_input(self) -> None:
        """A limit that is not a number never reaches the use case."""
        outcome = run(
            [*ARGS, "--hard-s", "soon"], SpyService(report=make_report(TerminalReason.COMPLETED))
        )
        self.assertEqual(outcome.code, 2, msg="invalid input exits 2")
        self.assertIn("--hard-s", outcome.stderr, msg="the flag is named")

    def test_a_blank_task_or_non_positive_limit_is_invalid_input(self) -> None:
        """The domain refuses them; the command reports that as invalid input."""
        service = SpyService(report=make_report(TerminalReason.COMPLETED))
        for extra in (["--task", "  "], ["--hard-s", "0"], ["--memory-bytes", "-1"]):
            with self.subTest(extra=extra):
                outcome = run([*ARGS, *extra], service)
                self.assertEqual(outcome.code, 2, msg="invalid input exits 2")
                self.assertEqual(service.specs, [], msg="the use case never ran")

    def test_a_directory_that_is_not_a_git_tree_exits_two(self) -> None:
        """WORKSPACE_NOT_GIT is a usage error and says so."""
        outcome = run(ARGS, SpyService(error=WorkspaceNotGitError("not a work tree")))
        self.assertEqual(outcome.code, 2, msg="WORKSPACE_NOT_GIT exits 2")
        self.assertIn("WORKSPACE_NOT_GIT", outcome.stderr, msg="the code is named")
        self.assertEqual(outcome.stdout, "", msg="no report was made")


class FailureTests(unittest.TestCase):
    """Errors that are not the caller's fault still end visibly and non-zero."""

    def test_known_errors_exit_one_with_their_message(self) -> None:
        """Workspace and store errors keep their own message."""
        errors = (
            WorkspaceError("git worktree add failed: boom"),
            RunStoreError("database is locked"),
        )
        for error in errors:
            with self.subTest(error=type(error).__name__):
                outcome = run(ARGS, SpyService(error=error))
                self.assertEqual(outcome.code, 1, msg="a failure exits 1")
                self.assertIn(str(error), outcome.stderr, msg="the reason is shown")
                self.assertNotIn("Traceback", outcome.stderr, msg="not a bare stack trace")

    def test_settings_that_cannot_be_loaded_exit_one_before_any_run(self) -> None:
        """The factory fails: the key named by the error is on stderr."""
        outcome = run(ARGS, SettingsError("settings: watchdog.hard_s: not a number"))
        self.assertEqual(outcome.code, 1, msg="a settings failure exits 1")
        self.assertIn("watchdog.hard_s", outcome.stderr, msg="the key is shown")
        self.assertEqual(outcome.stdout, "", msg="no report")

    def test_an_unexpected_error_is_named_and_exits_one(self) -> None:
        """Nothing is swallowed: the type and text reach stderr."""
        outcome = run(ARGS, SpyService(error=OSError("disk gone")))
        self.assertEqual(outcome.code, 1, msg="an unexpected failure exits 1")
        self.assertIn("OSError", outcome.stderr, msg="the type is shown")
        self.assertIn("disk gone", outcome.stderr, msg="the text is shown")


class LazinessTests(unittest.TestCase):
    """Commands that do not run a task must not load settings or build adapters."""

    def test_version_and_help_do_not_build_the_service(self) -> None:
        """`--version` and `--help` work even when settings are broken."""
        for argv in (["--version"], ["--help"]):
            with self.subTest(argv=argv):
                outcome = run(argv, SettingsError("settings: broken"))
                self.assertEqual(outcome.code, 0, msg="still succeeds")
                self.assertEqual(outcome.factory_calls, 0, msg="the factory is not called")


if __name__ == "__main__":
    unittest.main()
