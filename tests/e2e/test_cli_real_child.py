"""The command line as a real process: the shipped console entry point on a temporary account.

Owner decision 2026-11-22 (KICKOFF-13): the commands are also checked as a child process, so that
exit codes, standard output and standard error are seen as a caller sees them. The account is a
temporary folder; the worker is the fake `claude` of the other end-to-end tests.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.e2e import test_walking_skeleton as skeleton
from tests.e2e.test_background_runs import SLOW, BackgroundCase
from tests.support import cli_real_child
from tests.support.cli_failure import failure_of

ROOT = Path(__file__).resolve().parents[2]
MISSING_RUN = "0b8e5f4a-3c1d-4e2a-9b7c-1d2e3f4a5b6c"
EXIT_PENDING = 3
EXIT_STOPPED = 13
EXIT_UNKNOWN_RUN = 21
EXIT_REFUSED_ARGUMENT = 2


def child(home: str, *argv: str, timeout_s: float = 60.0) -> skeleton.Outcome:
    """Run `agentsd <argv>` as a child process whose account is `home`."""
    environment = {**os.environ, cli_real_child.HOME_ENV: home}
    done = subprocess.run(  # noqa: S603 - fixed test module, no shell
        [sys.executable, "-m", "tests.support.cli_real_child", *argv],
        capture_output=True,
        text=True,
        env=environment,
        cwd=ROOT,
        timeout=timeout_s,
        check=False,
    )
    return skeleton.Outcome(done.returncode, done.stdout, done.stderr)


class RealCliCase(BackgroundCase):
    """A temporary account whose commands run as child processes."""

    def setUp(self) -> None:
        """Keep the user settings where the shipped composition root reads them."""
        super().setUp()
        self.settings_file = self.home / ".config" / "agents-dispatcher" / "settings.toml"
        self.settings_file.parent.mkdir(parents=True)
        self.settings()
        self.install(SLOW)

    def run_cli(self, *argv: str) -> skeleton.Outcome:
        """Run one command as a child process against the temporary account."""
        return child(str(self.home), *argv)

    def start_run(self) -> str:
        """Start the test task and return its run id."""
        outcome = self.run_cli(
            "start", "--repo", str(self.repo), "--task", skeleton.TASK, "--task-type", "implement"
        )
        self.assertEqual(outcome.code, 0, msg=f"the run starts: {outcome.stderr}")
        self.assertEqual(set(json.loads(outcome.stdout)), {"run_id", "state"}, msg="one object")
        return str(json.loads(outcome.stdout)["run_id"])

    def supervisor_gone(self, run_id: str) -> None:
        """Wait until the run's supervisor has exited, so the account can be removed."""
        self.until(
            lambda: not json.loads(self.run_cli("status", run_id).stdout)["owner_alive"],
            "the supervisor to exit",
        )


class RunLifecycleTests(RealCliCase):
    """start, status, wait, stop and report, each seen from outside."""

    def test_a_run_is_started_followed_stopped_and_reported(self) -> None:
        """The exit code and the object of every step."""
        run_id = self.start_run()
        self.running(run_id)
        status = self.run_cli("status", run_id)
        self.assertEqual(status.code, 0, msg="status exits 0 for any state")
        self.assertEqual(json.loads(status.stdout)["run"]["run_id"], run_id, msg="the run")
        pending = self.run_cli("wait", run_id, "--timeout-s", "0.5")
        self.assertEqual(pending.code, EXIT_PENDING, msg="not ended yet")
        self.assertEqual(json.loads(pending.stdout)["result"], "PENDING", msg="pending")
        stopped = self.run_cli("stop", run_id, "--reason", "enough")
        self.assertEqual(stopped.code, 0, msg=f"the stop is accepted: {stopped.stderr}")
        waited = self.run_cli("wait", run_id)
        self.assertEqual(waited.code, EXIT_STOPPED, msg="the run ended STOPPED")
        self.assertEqual(json.loads(waited.stdout)["result"], "READY", msg="ready")
        report = self.run_cli("report", run_id)
        self.assertEqual(report.code, 0, msg="report exits 0 for an ended run")
        self.assertEqual(json.loads(report.stdout)["terminal_reason"], "STOPPED", msg="stopped")
        self.supervisor_gone(run_id)

    def test_status_in_text_format_prints_lines_for_a_person(self) -> None:
        """`--format text` is text on stdout, with the same exit code."""
        run_id = self.start_run()
        self.running(run_id)
        text = self.run_cli("status", "--format", "text", run_id)
        self.assertEqual(text.code, 0, msg="the exit code is the same")
        self.assertTrue(text.stdout.startswith(f"run {run_id} is "), msg="the headline")
        self.run_cli("stop", run_id, "--reason", "done with the test")
        self.supervisor_gone(run_id)


class FailureTests(RealCliCase):
    """Failures: the object on stdout, the line on stderr, the exit code."""

    def test_an_unknown_run_exits_twenty_one_with_its_object(self) -> None:
        """`UNKNOWN_RUN` on stdout, and the stderr line is kept."""
        outcome = self.run_cli("status", MISSING_RUN)
        self.assertEqual(outcome.code, EXIT_UNKNOWN_RUN, msg="unknown run")
        self.assertEqual(failure_of(outcome.stdout)["code"], "UNKNOWN_RUN", msg="the code")
        self.assertTrue(outcome.stderr.startswith("agentsd: error: "), msg="the stderr line")

    def test_a_folder_that_is_not_a_git_tree_exits_two_with_its_object(self) -> None:
        """`WORKSPACE_NOT_GIT` on stdout."""
        with tempfile.TemporaryDirectory() as folder:
            outcome = self.run_cli(
                "start", "--repo", folder, "--task", skeleton.TASK, "--task-type", "implement"
            )
        self.assertEqual(outcome.code, EXIT_REFUSED_ARGUMENT, msg="invalid input")
        self.assertEqual(failure_of(outcome.stdout)["code"], "WORKSPACE_NOT_GIT", msg="the code")
        self.assertIn("WORKSPACE_NOT_GIT", outcome.stderr, msg="the stderr line names it")


class ChildSafeguardTests(unittest.TestCase):
    """The child cannot reach the real account."""

    def test_a_home_outside_the_temporary_folder_is_refused(self) -> None:
        """Exit 70 before anything is read."""
        outcome = child("/usr/local/nowhere", "status", MISSING_RUN)
        self.assertEqual(outcome.code, cli_real_child.EXIT_REFUSED, msg="refused")
        self.assertEqual(outcome.stdout, "", msg="nothing was printed on stdout")
        self.assertIn("must be a folder under", outcome.stderr, msg="the reason is given")

    def test_the_lookup_is_replaced_in_every_module_that_reads_it(self) -> None:
        """The modules the child patches are the modules whose source names the lookup."""
        named = sorted(
            f"agents_dispatcher.composition.{path.stem}"
            for path in (ROOT / "src" / "agents_dispatcher" / "composition").glob("*.py")
            if f"{cli_real_child.LOOKUP}()" in path.read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as folder:
            code = (
                "import json; from tests.support import cli_real_child as c; "
                f"print(json.dumps(c.patch_account_home({folder!r})))"
            )
            done = subprocess.run(  # noqa: S603 - fixed test code, no shell
                [sys.executable, "-c", code],
                capture_output=True,
                text=True,
                cwd=ROOT,
                check=False,
            )
        self.assertEqual(done.returncode, 0, msg=done.stderr)
        self.assertEqual(json.loads(done.stdout), named, msg="every reader is patched")


if __name__ == "__main__":
    unittest.main()
