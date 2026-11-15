"""End-to-end tests of background runs: start, wait, stop and report through the real supervisor.

The worker is the same fake `claude` as in the walking-skeleton tests. Every command goes through
`main` with the services the console script builds; the supervisor is a real detached process.
"""

import io
import json
import os
import signal
import subprocess
import sys
import time
import unittest
from collections.abc import Callable
from pathlib import Path

from agents_dispatcher.adapters.system.signal_interrupts import SignalInterrupts
from agents_dispatcher.adapters.system.sqlite_run_store import SqliteRunStore
from agents_dispatcher.composition.cli import build_services
from agents_dispatcher.domain.run import RunId, RunState
from agents_dispatcher.interfaces.cli.app import main
from tests.e2e import test_walking_skeleton as skeleton
from tests.support.fake_executable import FakeExecutable
from tests.support.git_repo import git, make_repo

SRC = Path(__file__).resolve().parents[2] / "src"
SLOW = "sleep 3\nprintf '%s\\n' x > done.txt\n" + skeleton.success_json("slow and done")
STARTER = """\
import io
import sys

sys.path.insert(0, {src!r})
from agents_dispatcher.adapters.system.settings_loader import load_settings, shipped_defaults_text
from agents_dispatcher.adapters.system.signal_interrupts import SignalInterrupts
from agents_dispatcher.composition.cli import build_services
from agents_dispatcher.interfaces.cli.app import main


class Version:
    def installed_version(self):
        return "0"


def services():
    settings = load_settings(
        defaults_text=shipped_defaults_text(),
        user_text=open({settings_file!r}, encoding="utf-8").read(),
        account_home={home!r},
    )
    return build_services(
        settings,
        account_home={home!r},
        account_user="example",
        interpreter=sys.executable,
        path={path!r},
        user_settings={settings_file!r},
    )


out = io.StringIO()
code = main(
    ["start", "--repo", {repo!r}, "--task", {task!r}, "--task-type", "implement"],
    out, sys.stderr, Version(), services, SignalInterrupts(),
)
print(out.getvalue())
sys.exit(code)
"""


class BackgroundCase(skeleton.SkeletonCase):
    """A temporary account whose commands run the way the console script runs them."""

    def install(self, body: str) -> None:
        """Install a fake claude with this body."""
        FakeExecutable("claude", skeleton.VERSION_GUARD + body, skeleton.SPEC).write_to(self.bin)

    def cli(self, *argv: str) -> skeleton.Outcome:
        """Run one command in this process against the temporary account."""
        stdout, stderr = io.StringIO(), io.StringIO()
        code = main(
            list(argv),
            stdout,
            stderr,
            skeleton.FixedVersion(),
            lambda: build_services(
                self.settings(),
                account_home=str(self.home),
                account_user="example",
                interpreter=sys.executable,
                path=f"{self.bin}:/usr/bin:/bin",
                user_settings=str(self.settings_file),
            ),
            SignalInterrupts(),
        )
        return skeleton.Outcome(code, stdout.getvalue(), stderr.getvalue())

    def start(self, repo: Path | None = None) -> str:
        """Start a run, check the acknowledgement and return its id."""
        outcome = self.cli(
            "start",
            "--repo",
            str(repo or self.repo),
            "--task",
            skeleton.TASK,
            "--task-type",
            "implement",
        )
        self.assertEqual(outcome.code, 0, msg=outcome.stderr)
        return str(json.loads(outcome.stdout)["run_id"])

    def store(self) -> SqliteRunStore:
        """Open the store the commands use."""
        return SqliteRunStore(f"{self.root}/state/runs.sqlite3", epoch_s=time.time, busy_s=2.0)

    def until(self, condition: Callable[[], bool], what: str, seconds: float = 30.0) -> None:
        """Poll until the condition holds or fail the test."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if condition():
                return
            time.sleep(0.1)
        self.fail(f"timed out waiting for {what}")

    def running(self, run_id: str) -> RunId:
        """Wait until the run's worker is running and return the run id."""
        rid = RunId(run_id)
        stored = self.store()
        self.until(lambda: stored.get(rid).state is RunState.RUNNING, "the worker to run")
        return rid


class StartWaitReportTests(BackgroundCase):
    """The three commands follow one run, and `run` prints what `report` prints."""

    def test_start_returns_at_once_and_wait_and_report_follow_the_run(self) -> None:
        """`start` does not wait for the worker; `wait` ends with the run; `report` prints it."""
        self.install(SLOW)
        began = time.monotonic()
        run_id = self.start()
        self.assertLess(time.monotonic() - began, 2.5, msg="start came back before the worker")
        self.assertEqual(self.cli("report", run_id).code, 3, msg="an unfinished run is pending")
        waited = self.cli("wait", run_id)
        self.assertEqual(waited.code, 0, msg=waited.stderr)
        self.assertEqual(json.loads(waited.stdout)["result"], "READY", msg="ready")
        report = self.cli("report", run_id)
        self.assertEqual(report.code, 0, msg=report.stderr)
        self.assertEqual(json.loads(report.stdout)["terminal_reason"], "COMPLETED", msg="reason")
        self.assertEqual(
            git(self.repo, "show", f"agentsd-run-{run_id}:done.txt"), "x\n", msg="the work landed"
        )

    def test_run_prints_exactly_the_text_report_prints_for_the_same_run(self) -> None:
        """Equal by construction (one stored text) and shown to be equal."""
        self.install(skeleton.success_json("done"))
        ran = self.cli(
            "run", "--repo", str(self.repo), "--task", skeleton.TASK, "--task-type", "implement"
        )
        self.assertEqual(ran.code, 0, msg=ran.stderr)
        run_id = str(json.loads(ran.stdout)["run_id"])
        self.assertEqual(self.cli("report", run_id).stdout, ran.stdout, msg="same text")

    def test_the_report_names_the_model_the_cli_result_reports(self) -> None:
        """`modelUsage` of a real-shaped result fills `worker.model`."""
        self.install(
            'printf \'{"type":"result","subtype":"success","is_error":false,"result":"ok",'
            '"modelUsage":{"claude-sonnet-5-5":{"inputTokens":2,"outputTokens":6,'
            '"cacheReadInputTokens":0,"cacheCreationInputTokens":0,"webSearchRequests":0,'
            '"costUSD":0}}}\\n\'\n'
        )
        ran = self.cli(
            "run", "--repo", str(self.repo), "--task", skeleton.TASK, "--task-type", "implement"
        )
        self.assertEqual(ran.code, 0, msg=ran.stderr)
        self.assertEqual(
            json.loads(ran.stdout)["worker"]["model"], "claude-sonnet-5-5", msg="model in report"
        )

    def test_status_shows_a_live_owner_and_an_unknown_id_exits_twenty_one(self) -> None:
        """The owner lock is what `owner_alive` reports."""
        self.install(SLOW)
        run_id = self.start()
        status = json.loads(self.cli("status", run_id).stdout)
        self.assertTrue(status["owner_alive"], msg="the supervisor holds the run")
        self.cli("wait", run_id)
        unknown = self.cli("status", "3f2b8c1e-5d4a-4b7e-9c3a-1f2e3d4c5b6a")
        self.assertEqual(unknown.code, 21, msg="unknown run")


class InputBoundTests(BackgroundCase):
    """A task over `security.input_bytes` creates nothing."""

    def test_start_and_run_refuse_an_oversized_task_with_exit_two(self) -> None:
        """No run row is made, so no run folder or branch can exist either."""
        self.install(skeleton.success_json("never"))
        self.extra_settings = "[security]\ninput_bytes = 16\n"
        for command in ("start", "run"):
            outcome = self.cli(command, "--repo", str(self.repo), "--task", "x" * 17)
            self.assertEqual(outcome.code, 2, msg=f"{command}: {outcome.stderr}")
            self.assertIn("16", outcome.stderr, msg=f"{command}: the bound is named")
        self.assertEqual(git(self.repo, "branch", "--list", "agentsd-run-*"), "", msg="no branch")


class SurvivalTests(BackgroundCase):
    """A run does not depend on the process that started it."""

    def test_a_run_goes_on_after_the_process_that_started_it_has_exited(self) -> None:
        """`start` runs in a child that exits; the run still completes."""
        self.install(SLOW)
        self.settings()
        script = self.root / "starter.py"
        script.write_text(
            STARTER.format(
                src=str(SRC),
                settings_file=str(self.settings_file),
                home=str(self.home),
                path=f"{self.bin}:/usr/bin:/bin",
                repo=str(self.repo),
                task=skeleton.TASK,
            ),
            encoding="utf-8",
        )
        completed = subprocess.run(  # noqa: S603 - argv array of this interpreter, no shell
            [sys.executable, "-I", str(script)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, msg=completed.stderr)
        run_id = str(json.loads(completed.stdout)["run_id"])
        waited = self.cli("wait", run_id)
        self.assertEqual(waited.code, 0, msg=waited.stderr)

    def test_a_killed_supervisor_is_noticed_by_wait_and_its_worker_is_stopped(self) -> None:
        """SIGKILL of the supervisor: the run ends ORPHANED (14) and nothing stays running."""
        self.install("sleep 300\n")
        run_id = self.start()
        rid = self.running(run_id)
        pid = self.store().get(rid).supervisor_pid
        self.assertIsNotNone(pid, msg="the supervisor is recorded")
        os.kill(int(pid or 0), signal.SIGKILL)
        waited = self.cli("wait", run_id)
        self.assertEqual(waited.code, 14, msg=waited.stderr)
        self.assertEqual(self.marked_processes(run_id), [], msg="no worker process is left")


class StopTests(BackgroundCase):
    """`stop` ends a running worker and the run still ends cleanly."""

    def test_stop_ends_the_run_as_stopped_and_wait_exits_thirteen(self) -> None:
        """The request goes through the store to the supervisor."""
        self.install("echo partial > partial.txt\nsleep 300\n")
        run_id = self.start()
        self.running(run_id)
        stopped = self.cli("stop", run_id, "--reason", "enough")
        self.assertEqual(stopped.code, 0, msg=stopped.stderr)
        self.assertEqual(json.loads(stopped.stdout)["disposition"], "EFFECTIVE", msg="effective")
        waited = self.cli("wait", run_id)
        self.assertEqual(waited.code, 13, msg=waited.stderr)
        self.assertEqual(self.marked_processes(run_id), [], msg="nothing is left running")


class ConcurrencyAndConfinementTests(BackgroundCase):
    """Runs side by side, and what the worker may not read."""

    def test_two_runs_at_once_both_complete_on_their_own_branches(self) -> None:
        """Separate supervisors, separate worktrees."""
        self.install(SLOW)
        other = Path(make_repo(self.root / "other"))
        first, second = self.start(), self.start(other)
        self.assertNotEqual(first, second, msg="two ids")
        for run_id in (first, second):
            self.assertEqual(self.cli("wait", run_id).code, 0, msg=f"{run_id} completed")
        self.assertIn(f"agentsd-run-{first}", git(self.repo, "branch", "--list"), msg="branch 1")
        self.assertIn(f"agentsd-run-{second}", git(other, "branch", "--list"), msg="branch 2")

    def test_the_worker_cannot_read_the_run_store_or_the_supervisor_log(self) -> None:
        """Control files stay out of the worker's reach."""
        state = self.root / "state"
        body = (
            f'if cat "{state}/runs.sqlite3" >/dev/null 2>&1; then a=READ; else a=DENIED; fi\n'
            f'if ls "{state}/logs" >/dev/null 2>&1; then b=READ; else b=DENIED; fi\n'
            + skeleton.success_json("store:$a logs:$b")
        )
        self.install(body)
        outcome = self.cli(
            "run", "--repo", str(self.repo), "--task", skeleton.TASK, "--task-type", "implement"
        )
        self.assertEqual(outcome.code, 0, msg=outcome.stderr)
        self.assertEqual(
            json.loads(outcome.stdout)["result_text"],
            "store:DENIED logs:DENIED",
            msg="the worker's own view agrees",
        )


if __name__ == "__main__":
    unittest.main()
