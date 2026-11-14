"""End-to-end tests of `agentsd run` through the real Seatbelt profile, proxy and Git worktree.

The worker is a fake `claude` script. Its output shape is the documented `--output-format json`
result object of Claude Code 2.1.304 (spec excerpt); the one real run is a separate, manual step.
"""

import io
import json
import sys
import tempfile
import unittest
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from agents_dispatcher.adapters.system.process_table import LibprocProcessTable
from agents_dispatcher.adapters.system.seatbelt_confinement import MARKER_ENV
from agents_dispatcher.adapters.system.settings_loader import load_settings, shipped_defaults_text
from agents_dispatcher.adapters.system.signal_interrupts import SignalInterrupts
from agents_dispatcher.application.settings import Settings
from agents_dispatcher.composition.cli import build_services
from agents_dispatcher.interfaces.cli.app import main
from tests.support.fake_executable import FakeExecutable, SourceRef
from tests.support.git_repo import checkout_bytes, git, make_repo
from tests.support.hard_timeout import HardTimeoutCase

SPEC = SourceRef(
    kind="spec_excerpt",
    cli_name="claude",
    cli_version="2.1.304",
    excerpt=(
        "claude -p --output-format json prints one result object with type, subtype, is_error "
        "and result; claude --version prints '<version> (Claude Code)'."
    ),
)
VERSION_GUARD = 'case "$1" in --version) echo "2.1.304 (Claude Code)"; exit 0;; esac\n'
TASK = "add a greeting file"


def success_json(result: str) -> str:
    """Return shell code that prints a success result object with this text (shell-expanded)."""
    return (
        'printf \'{"type":"result","subtype":"success","is_error":false,'
        f'"result":"%s"}}\\n\' "{result}"\n'
    )


@dataclass(frozen=True)
class Outcome:
    """What one `agentsd run` produced."""

    code: int
    stdout: str
    stderr: str

    @property
    def report(self) -> dict[str, object]:
        """The JSON report printed on stdout."""
        loaded = json.loads(self.stdout)
        if not isinstance(loaded, dict):
            message = f"the report is not an object: {self.stdout!r}"
            raise TypeError(message)
        return loaded

    @property
    def limit_hit(self) -> object:
        """The limit the report says was hit."""
        limits = self.report["limits"]
        if not isinstance(limits, dict):
            message = f"limits is not an object: {limits!r}"
            raise TypeError(message)
        return limits["hit"]


class FixedVersion:
    """A version source; these tests never print the version."""

    def installed_version(self) -> str | None:
        """Return a fixed version."""
        return "0.0.0-test"


class SkeletonCase(HardTimeoutCase):
    """A temporary account with a repository, a fake claude and short stop timings."""

    def setUp(self) -> None:
        """Create the directories every test needs."""
        super().setUp()
        self.extra_settings = ""  # more user-file TOML, appended by `settings()`
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        self.home = self.root / "home"
        self.bin = self.root / "bin"
        self.outside = self.root / "outside"
        for folder in (self.home, self.bin, self.outside):
            folder.mkdir()
        self.repo = Path(make_repo(self.root / "repo"))
        self.settings_file = self.home / "settings.toml"

    def settings(self) -> Settings:
        """Load settings that keep state under the temporary root and stop quickly."""
        user = (
            f'[store]\npath = "{self.root}/state"\n'
            f'[run_area]\npath = "{self.root}/runs"\n'
            f'[profiles._template]\nexecutable = "{self.bin}/claude"\n'
            '[watchdog]\nterm_grace_s = "1"\nkill_wait_s = "1"\nscan_s = "0.2"\n'
            f"{self.extra_settings}"
        )
        self.settings_file.write_text(user, encoding="utf-8")
        return load_settings(
            defaults_text=shipped_defaults_text(), user_text=user, account_home=str(self.home)
        )

    def agentsd(self, body: str, argv: Sequence[str] | None = None) -> Outcome:
        """Install a fake claude with this body and run `agentsd run` on the test repository."""
        FakeExecutable("claude", VERSION_GUARD + body, SPEC).write_to(self.bin)
        arguments = argv if argv is not None else ["--repo", str(self.repo), "--task", TASK]
        stdout, stderr = io.StringIO(), io.StringIO()
        code = main(
            ["run", *arguments],
            stdout,
            stderr,
            FixedVersion(),
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
        return Outcome(code, stdout.getvalue(), stderr.getvalue())

    def with_limit(self, *flags: str) -> list[str]:
        """Return the standard arguments followed by extra flags."""
        return ["--repo", str(self.repo), "--task", TASK, *flags]

    def run_folders(self) -> list[str]:
        """Return the run sandboxes still present in the run area."""
        area = self.root / "runs"
        return sorted(path.name for path in area.glob("agentsd-run-*")) if area.exists() else []

    @staticmethod
    def marked_processes(run_id: str) -> list[int]:
        """Return the pids that still carry the run marker."""
        table = LibprocProcessTable()
        return [
            pid
            for pid in table.all_pids()
            if (table.environment(pid) or {}).get(MARKER_ENV) == run_id
        ]


class CompletedRunTests(SkeletonCase):
    """A worker that does its job."""

    def test_the_worker_result_lands_on_a_branch_and_the_checkout_is_untouched(self) -> None:
        """Branch with the worker's file and the brief it received; the user's tree unchanged."""
        before_files = checkout_bytes(self.repo, ignore=(".git/",))
        before_head = git(self.repo, "rev-parse", "HEAD")
        outcome = self.agentsd(
            "brief=$(cat)\nprintf '%s\\n' \"$brief\" > greeting.txt\n"
            + success_json("Wrote the file.")
        )
        report = outcome.report
        self.assertEqual(outcome.code, 0, msg=outcome.stderr)
        self.assertEqual(report["terminal_reason"], "COMPLETED", msg="completed")
        branch = str(report["branch"])
        self.assertEqual(branch, f"agentsd-run-{report['run_id']}", msg="result branch name")
        self.assertEqual(report["commits_on_branch"], 1, msg="one commit")
        self.assertEqual(
            git(self.repo, "show", f"{branch}:greeting.txt"),
            f"{TASK}\n",
            msg="the brief reached the worker on standard input",
        )
        self.assertEqual(checkout_bytes(self.repo, ignore=(".git/",)), before_files, msg="files")
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), before_head, msg="HEAD unchanged")
        self.assertEqual(git(self.repo, "status", "--porcelain"), "", msg="tree still clean")
        self.assertEqual(self.run_folders(), [], msg="the sandbox is removed after the run")

    def test_a_worker_that_changes_nothing_leaves_no_branch(self) -> None:
        """Q4: no branch, `branch: null`, and a note that says why."""
        outcome = self.agentsd(success_json("Nothing to do."))
        report = outcome.report
        self.assertEqual(outcome.code, 0, msg=outcome.stderr)
        self.assertIsNone(report["branch"], msg="no branch")
        self.assertIn("worker made no changes", str(report["notes"]), msg="the note")
        self.assertEqual(git(self.repo, "branch", "--list", "agentsd-run-*"), "", msg="no ref")


class ConfinementTests(SkeletonCase):
    """What the worker may not do, shown by the file system and not by its own report."""

    def test_writes_outside_the_sandbox_and_reads_of_credentials_are_denied(self) -> None:
        """The fake tries to write into the user's checkout and elsewhere, and to read ~/.ssh."""
        ssh = self.home / ".ssh"
        ssh.mkdir()
        (ssh / "id_example").write_text("not-a-real-key\n", encoding="utf-8")
        body = (
            f'if echo x > "{self.repo}/leak.txt" 2>/dev/null; then a=WROTE; else a=DENIED; fi\n'
            f'if echo x > "{self.outside}/leak.txt" 2>/dev/null; then b=WROTE; else b=DENIED; fi\n'
            f'if cat "{ssh}/id_example" >/dev/null 2>&1; then c=READ; else c=DENIED; fi\n'
            + success_json("repo:$a outside:$b ssh:$c")
        )
        outcome = self.agentsd(body)
        self.assertEqual(outcome.code, 0, msg=outcome.stderr)
        self.assertFalse((self.repo / "leak.txt").exists(), msg="nothing written to the checkout")
        self.assertFalse((self.outside / "leak.txt").exists(), msg="nothing written outside")
        self.assertEqual(
            outcome.report["result_text"],
            "repo:DENIED outside:DENIED ssh:DENIED",
            msg="the worker's own view agrees with the file system",
        )


class LimitAndFailureTests(SkeletonCase):
    """Runs that do not complete."""

    def test_the_hard_time_limit_stops_the_worker_and_exits_sixteen(self) -> None:
        """RESOURCE_LIMIT with the time key named; nothing is left running."""
        outcome = self.agentsd("sleep 300\n", self.with_limit("--hard-s", "2"))
        self.assertEqual(outcome.code, 16, msg=outcome.stderr)
        self.assertEqual(outcome.report["terminal_reason"], "RESOURCE_LIMIT", msg="reason")
        self.assertEqual(outcome.limit_hit, "watchdog.hard_s", msg="the limit that was hit")
        self.assertEqual(
            self.marked_processes(str(outcome.report["run_id"])), [], msg="nothing left"
        )

    def test_the_memory_limit_stops_the_worker_and_exits_sixteen(self) -> None:
        """RESOURCE_LIMIT with the memory key named; nothing is left running."""
        grabber = (
            f"{sys.executable} -c 'import time; x = b\"x\" * (600 * 1024 * 1024); time.sleep(60)'\n"
        )
        outcome = self.agentsd(grabber, self.with_limit("--memory-bytes", "200000000"))
        self.assertEqual(outcome.code, 16, msg=outcome.stderr)
        self.assertEqual(outcome.limit_hit, "resources.run.memory_bytes", msg="memory limit")
        self.assertEqual(
            self.marked_processes(str(outcome.report["run_id"])), [], msg="nothing left"
        )

    def test_a_worker_that_reports_an_error_is_a_failed_run(self) -> None:
        """Exit 10, the reason on stderr, the report still printed."""
        outcome = self.agentsd(
            'printf \'{"type":"result","subtype":"error_max_turns",'
            '"is_error":true,"result":"Out of turns"}\\n\'\nexit 1\n'
        )
        self.assertEqual(outcome.code, 10, msg=outcome.stderr)
        self.assertEqual(outcome.report["terminal_reason"], "FAILED", msg="reason")
        self.assertIn("Out of turns", str(outcome.report["result_text"]), msg="reason kept")
        self.assertIn("FAILED", outcome.stderr, msg="message on stderr")

    def test_a_directory_that_is_not_a_git_tree_exits_two_and_creates_nothing(self) -> None:
        """WORKSPACE_NOT_GIT, no sandbox, no report."""
        plain = self.root / "plain"
        plain.mkdir()
        outcome = self.agentsd(success_json("unused"), ["--repo", str(plain), "--task", TASK])
        self.assertEqual(outcome.code, 2, msg=outcome.stderr)
        self.assertIn("WORKSPACE_NOT_GIT", outcome.stderr, msg="named on stderr")
        self.assertEqual(outcome.stdout, "", msg="no report")
        self.assertEqual(self.run_folders(), [], msg="no sandbox was created")


if __name__ == "__main__":
    unittest.main()
