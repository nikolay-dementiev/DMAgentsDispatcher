"""End-to-end tests of the detached supervisor: one accepted run, run to its end by its own process.

The worker is a fake `claude` (output shape per the Claude Code 2.1.304 spec excerpt used by the
walking-skeleton tests). The supervisor runs from its real bootstrap file with a scrubbed
environment, exactly as `start` launches it.
"""

import json
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from agents_dispatcher.adapters.system.detached_launcher import (
    DetachedLauncherConfig,
    DetachedSupervisorLauncher,
)
from agents_dispatcher.adapters.system.process_table import LibprocProcessTable
from agents_dispatcher.adapters.system.seatbelt_confinement import MARKER_ENV
from agents_dispatcher.adapters.system.sqlite_run_store import SqliteRunStore
from agents_dispatcher.application.ports import AcceptedRun, FinishRecord
from agents_dispatcher.domain.run import Limits, RunId, RunState, TerminalReason
from agents_dispatcher.domain.views import StopDisposition
from tests.support.fake_executable import FakeExecutable, SourceRef
from tests.support.git_repo import git, make_repo
from tests.support.hard_timeout import HardTimeoutCase
from tests.support.routing import ROUTING

SRC = Path(__file__).resolve().parents[2] / "src"
BOOTSTRAP_RELATIVE = "agents_dispatcher/composition/supervisor_bootstrap.py"
SPEC = SourceRef(
    kind="spec_excerpt",
    cli_name="claude",
    cli_version="2.1.304",
    excerpt=(
        "claude -p --output-format json prints one result object with type, subtype, is_error "
        "and result; claude --version prints '<version> (Claude Code)'."
    ),
)
BODY = (
    'case "$1" in --version) echo "2.1.304 (Claude Code)"; exit 0;; esac\n'
    "brief=$(cat)\nprintf '%s\\n' \"$brief\" > greeting.txt\n"
    'printf \'{"type":"result","subtype":"success","is_error":false,"result":"done"}\\n\'\n'
)
TASK = "add a greeting file"


class SupervisorCase(HardTimeoutCase):
    """A temporary account with a repository, a fake claude and an accepted run."""

    def setUp(self) -> None:
        """Create the account, the repository, the settings file and the store."""
        super().setUp()
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        self.home = self.root / "home"
        self.bin = self.root / "bin"
        for folder in (self.home / ".config" / "agents-dispatcher", self.bin):
            folder.mkdir(parents=True)
        self.repo = Path(make_repo(self.root / "repo"))
        self.settings_file = self.home / ".config" / "agents-dispatcher" / "settings.toml"
        self.settings_file.write_text(
            f'[store]\npath = "{self.root}/state"\n'
            f'[run_area]\npath = "{self.root}/runs"\n'
            f'[profiles._template]\nexecutable = "{self.bin}/claude"\n'
            '[watchdog]\nterm_grace_s = "1"\nkill_wait_s = "1"\nscan_s = "0.2"\n',
            encoding="utf-8",
        )
        FakeExecutable("claude", BODY, SPEC).write_to(self.bin)
        self.run_id = RunId(str(uuid.uuid4()))
        self.store = SqliteRunStore(
            f"{self.root}/state/runs.sqlite3",
            epoch_s=time.time,
            busy_s=2.0,
        )
        self.store.accept(
            AcceptedRun(self.run_id, str(self.repo), TASK, Limits(120.0, 2 * 1024**3), ROUTING)
        )

    def supervisor_argv(self, tree: Path, bootstrap: Path) -> list[str]:
        """Return the command line `start` would use for this run."""
        return [
            sys.executable,
            "-I",
            "-B",
            str(bootstrap),
            str(tree),
            "--run-id",
            self.run_id.value,
            "--store-path",
            f"{self.root}/state",
            "--account-home",
            str(self.home),
            "--account-user",
            "example",
            "--user-settings",
            str(self.settings_file),
        ]

    def run_supervisor(
        self, tree: Path = SRC, brief: str = TASK
    ) -> subprocess.CompletedProcess[str]:
        """Run the supervisor to its end with the exact environment `start` gives it."""
        bootstrap = tree / BOOTSTRAP_RELATIVE
        return subprocess.run(  # noqa: S603 - argv array of this interpreter, no shell
            self.supervisor_argv(tree, bootstrap),
            input=brief,
            capture_output=True,
            text=True,
            timeout=80,
            check=False,
            cwd="/",
            env={
                "HOME": str(self.home),
                "USER": "example",
                "LOGNAME": "example",
                "PATH": f"{self.bin}:/usr/bin:/bin",
            },
        )

    def log_text(self) -> str:
        """Return the supervisor log of the run."""
        return (self.root / "state" / "logs" / f"{self.run_id.value}.log").read_text(
            encoding="utf-8"
        )


class SupervisorRunTests(SupervisorCase):
    """The supervisor takes the accepted run to a stored, terminal result."""

    def test_the_accepted_run_ends_completed_with_its_report_and_branch(self) -> None:
        """Everything `report` needs is in the store when the supervisor exits."""
        done = self.run_supervisor()
        self.assertEqual(done.returncode, 0, msg=done.stderr)
        found = self.store.find(self.run_id)
        self.assertIsNotNone(found, msg="the run is stored")
        assert found is not None  # noqa: S101 - narrows the type for the checker in a test
        self.assertEqual(found.state, RunState.TERMINAL, msg="terminal")
        self.assertEqual(found.terminal_reason, TerminalReason.COMPLETED, msg="completed")
        report = json.loads(found.report_json or "{}")
        self.assertEqual(report["terminal_reason"], "COMPLETED", msg="the stored report")
        self.assertEqual(
            git(self.repo, "show", f"{self.run_id.branch_name}:greeting.txt"),
            f"{TASK}\n",
            msg="the brief reached the worker through the supervisor's standard input",
        )

    def test_the_log_is_private_names_the_package_and_records_proxy_activity(self) -> None:
        """The log is the only trace of a run that nobody watched."""
        self.run_supervisor()
        log_path = self.root / "state" / "logs" / f"{self.run_id.value}.log"
        self.assertEqual(stat.S_IMODE(log_path.stat().st_mode), 0o600, msg="log mode")
        text = self.log_text()
        self.assertIn(f"package: {SRC}", text, msg="the package copy that runs")
        self.assertIn("proxy decisions: 0", text, msg="the proxy summary is written")

    def test_a_run_that_was_already_closed_is_not_taken_over(self) -> None:
        """If `start` gave up first, the late supervisor exits non-zero and does nothing."""
        lost = self.store.find(self.run_id)
        assert lost is not None  # noqa: S101 - narrows the type for the checker in a test
        self.store.finish_unclaimed(
            self.run_id,
            FinishRecord(TerminalReason.LAUNCH_LOST, None, 0, None, None, 0.0, "{}"),
        )
        done = self.run_supervisor()
        self.assertNotEqual(done.returncode, 0, msg="a refusal is an error exit")
        self.assertIn("no longer waiting", self.log_text(), msg="the log says why")
        self.assertEqual(git(self.repo, "branch", "--list", "agentsd-run-*"), "", msg="no work")


class FaultRecordingTests(SupervisorCase):
    """A supervisor that gives up says why in the run row before it exits."""

    def test_a_missing_worker_executable_is_recorded_as_the_fault(self) -> None:
        """The cause is a settings message that names the executable."""
        (self.bin / "claude").unlink()
        done = self.run_supervisor()
        self.assertNotEqual(done.returncode, 0, msg="the supervisor failed")
        fault = self.store.get(self.run_id).fault
        self.assertIn("not an executable file", str(fault), msg="the cause is in the row")

    def test_settings_that_do_not_load_are_recorded_as_the_fault(self) -> None:
        """Before the log opens, the store path argument is enough to leave the cause."""
        self.settings_file.write_text("this is [not toml", encoding="utf-8")
        done = self.run_supervisor()
        self.assertNotEqual(done.returncode, 0, msg="the supervisor failed")
        self.assertIn("settings", str(self.store.get(self.run_id).fault), msg="cause in the row")

    def test_a_store_path_that_differs_from_the_settings_is_the_recorded_fault(self) -> None:
        """The row that `start` made is where the launcher's path points; the cause goes there."""
        other = SqliteRunStore(f"{self.root}/other/runs.sqlite3", epoch_s=time.time, busy_s=2.0)
        other.accept(
            AcceptedRun(self.run_id, str(self.repo), TASK, Limits(120.0, 2 * 1024**3), ROUTING)
        )
        bootstrap = SRC / BOOTSTRAP_RELATIVE
        argv = self.supervisor_argv(SRC, bootstrap)
        argv[argv.index("--store-path") + 1] = f"{self.root}/other"
        done = subprocess.run(  # noqa: S603 - argv array of this interpreter, no shell
            argv,
            input=TASK,
            capture_output=True,
            text=True,
            timeout=80,
            check=False,
            cwd="/",
            env={"HOME": str(self.home), "USER": "example", "PATH": f"{self.bin}:/usr/bin:/bin"},
        )
        self.assertNotEqual(done.returncode, 0, msg="the supervisor failed")
        fault = str(other.get(self.run_id).fault)
        self.assertIn(f"{self.root}/other", fault, msg="the path the launcher passed is named")
        self.assertIn(f"{self.root}/state", fault, msg="the path the settings load is named")
        self.assertIsNone(self.store.get(self.run_id).fault, msg="the other store is untouched")
        self.assertEqual(
            self.store.get(self.run_id).state, RunState.ACCEPTED, msg="and nothing was claimed"
        )

    def test_the_fault_is_bounded_by_the_summary_limit(self) -> None:
        """A long message is cut, not stored whole."""
        text = self.settings_file.read_text(encoding="utf-8")
        long_path = "/" + "a" * 5000
        self.settings_file.write_text(
            text.replace(f"{self.bin}/claude", long_path), encoding="utf-8"
        )
        self.run_supervisor()
        fault = self.store.get(self.run_id).fault
        self.assertIsNotNone(fault, msg="a fault was recorded")
        self.assertLessEqual(len((fault or "").encode("utf-8")), 1024, msg="summary_bytes")
        self.assertIn("settings: profiles", fault or "", msg="the start of the cause is kept")


class SupervisorDeathTests(SupervisorCase):
    """What a signal to the supervisor does to the worker, with no caller running `wait`."""

    GONE_WITHIN_S = 5.0

    def start_slow_supervisor(self, work: str = "") -> subprocess.Popen[str]:
        """Start the real supervisor on a worker that does `work`, then sleeps; wait for RUNNING."""
        FakeExecutable(
            "claude",
            'case "$1" in --version) echo "2.1.304 (Claude Code)"; exit 0;; esac\n'
            f"cat > /dev/null\n{work}sleep 300\n",
            SPEC,
        ).write_to(self.bin)
        process = subprocess.Popen(  # noqa: S603 - argv array of this interpreter, no shell
            self.supervisor_argv(SRC, SRC / BOOTSTRAP_RELATIVE),
            stdin=subprocess.PIPE,
            text=True,
            cwd="/",
            env={
                "HOME": str(self.home),
                "USER": "example",
                "LOGNAME": "example",
                "PATH": f"{self.bin}:/usr/bin:/bin",
            },
            start_new_session=True,
        )
        self.addCleanup(self.reap, process)
        if process.stdin is not None:
            process.stdin.write(TASK)
            process.stdin.close()
        deadline = time.monotonic() + 30.0
        while self.store.get(self.run_id).state is not RunState.RUNNING:
            self.assertLess(time.monotonic(), deadline, msg="the worker never started running")
            time.sleep(0.05)
        return process

    def reap(self, process: subprocess.Popen[str]) -> None:
        """Make sure neither the supervisor nor the worker group outlives the test."""
        process.kill()
        process.wait(timeout=10)
        pgid = self.store.get(self.run_id).pgid
        table = LibprocProcessTable()
        for member in table.group_members(pgid or 0) if pgid else []:
            subprocess.run(["/bin/kill", "-9", str(member)], check=False)  # noqa: S603

    def group_members(self) -> list[int]:
        """Return the pids still in the worker's process group."""
        pgid = self.store.get(self.run_id).pgid
        return LibprocProcessTable().group_members(pgid) if pgid else []

    def wait_until_gone(self) -> float:
        """Return the seconds until the worker group is empty; fail past the stated bound."""
        began = time.monotonic()
        while self.group_members():
            self.assertLess(time.monotonic() - began, self.GONE_WITHIN_S, msg="group still alive")
            time.sleep(0.05)
        return time.monotonic() - began

    def test_sigterm_stops_the_worker_and_the_run_ends_stopped_with_the_cause(self) -> None:
        """The supervisor treats SIGTERM as a request to stop: worker group, report, exit."""
        process = self.start_slow_supervisor()
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=30)
        stored = self.store.get(self.run_id)
        self.assertEqual(
            stored.state, RunState.TERMINAL, msg="the run was closed by the supervisor"
        )
        self.assertEqual(stored.terminal_reason, TerminalReason.STOPPED, msg="STOPPED")
        notes = json.loads(str(stored.report_json))["notes"]
        self.assertIn(
            "stopped: supervisor received SIGTERM", notes, msg="the cause is in the notes"
        )
        self.assertEqual(self.group_members(), [], msg="no worker process is left")

    def test_sigterm_mid_run_seals_the_work_done_so_far_on_the_result_branch(self) -> None:
        """Same preservation as a stop through the store: the partial result is not lost."""
        process = self.start_slow_supervisor("echo partial > partial.txt\nsleep 1\n")
        time.sleep(1.5)  # the file is written; the worker is asleep in its long sleep
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=30)
        branch = self.run_id.branch_name
        self.assertEqual(
            git(self.repo, "show", f"{branch}:partial.txt"),
            "partial\n",
            msg="the worker's file is on the result branch",
        )
        report = json.loads(str(self.store.get(self.run_id).report_json))
        self.assertEqual(report["commits_on_branch"], 1, msg="the report counts the sealing commit")

    def test_sighup_is_treated_like_sigterm(self) -> None:
        """A closed terminal must not leave a worker behind either."""
        process = self.start_slow_supervisor()
        process.send_signal(signal.SIGHUP)
        process.wait(timeout=30)
        stored = self.store.get(self.run_id)
        self.assertEqual(stored.terminal_reason, TerminalReason.STOPPED, msg="STOPPED")
        notes = json.loads(str(stored.report_json))["notes"]
        self.assertIn("stopped: supervisor received SIGHUP", notes, msg="the cause is in the notes")

    def test_sigkill_leaves_no_worker_group_even_when_nobody_calls_wait(self) -> None:
        """Measured: the shim's dead-parent rule ends the group (about 0.6 s on this host)."""
        process = self.start_slow_supervisor()
        process.kill()
        process.wait(timeout=30)
        self.assertLess(self.wait_until_gone(), self.GONE_WITHIN_S, msg="group gone in time")
        self.assertEqual(
            self.store.get(self.run_id).state,
            RunState.RUNNING,
            msg="the store still says RUNNING: the next wait closes it as ORPHANED",
        )


class StopThroughTheStoreTests(SupervisorCase):
    """A stop request written to the store reaches a worker the supervisor is watching."""

    def test_a_stop_request_ends_a_running_worker_and_the_run_is_stopped(self) -> None:
        """The caller needs no connection to the supervisor: the store is the channel."""
        FakeExecutable(
            "claude",
            'case "$1" in --version) echo "2.1.304 (Claude Code)"; exit 0;; esac\n'
            "cat > /dev/null\nsleep 120\n",
            SPEC,
        ).write_to(self.bin)
        process = subprocess.Popen(  # noqa: S603 - argv array of this interpreter, no shell
            self.supervisor_argv(SRC, SRC / BOOTSTRAP_RELATIVE),
            stdin=subprocess.PIPE,
            text=True,
            cwd="/",
            env={
                "HOME": str(self.home),
                "USER": "example",
                "LOGNAME": "example",
                "PATH": f"{self.bin}:/usr/bin:/bin",
            },
        )
        self.addCleanup(process.wait)
        if process.stdin is not None:
            process.stdin.write(TASK)
            process.stdin.close()
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            found = self.store.find(self.run_id)
            if found is not None and found.state is RunState.RUNNING:
                break
            time.sleep(0.1)
        self.assertEqual(self.store.request_stop(self.run_id, "enough"), StopDisposition.EFFECTIVE)
        self.assertEqual(process.wait(timeout=60), 0, msg="the supervisor ended normally")
        found = self.store.find(self.run_id)
        self.assertEqual(found.terminal_reason if found else None, TerminalReason.STOPPED)
        marked = [
            pid
            for pid in LibprocProcessTable().all_pids()
            if (LibprocProcessTable().environment(pid) or {}).get(MARKER_ENV) == self.run_id.value
        ]
        self.assertEqual(marked, [], msg="no process of the run is left")


class SamePackageCopyTests(SupervisorCase):
    """The supervisor runs the package copy of its launcher, also from a non-installed tree."""

    def test_a_copied_tree_is_the_one_that_runs(self) -> None:
        """The launcher's package root is on argv; nothing in the environment can redirect it."""
        tree = self.root / "tree"
        shutil.copytree(
            SRC / "agents_dispatcher",
            tree / "agents_dispatcher",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        done = self.run_supervisor(tree=tree)
        self.assertEqual(done.returncode, 0, msg=done.stderr)
        text = self.log_text()
        self.assertIn(f"package: {tree}", text, msg="the copy ran")
        self.assertNotIn(f"package: {SRC}", text, msg="not the installed or source tree")

    def test_the_launcher_passes_its_own_package_root(self) -> None:
        """Through the real launcher the root of the launching copy reaches the supervisor."""
        config = DetachedLauncherConfig(
            interpreter=sys.executable,
            bootstrap=str(SRC / BOOTSTRAP_RELATIVE),
            leading_args=(str(SRC),),
            account_home=str(self.home),
            account_user="example",
            path=f"{self.bin}:/usr/bin:/bin",
            store_path=f"{self.root}/state",
            user_settings=str(self.settings_file),
            write_timeout_s=5.0,
        )
        DetachedSupervisorLauncher(config).launch(self.run_id, TASK)
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            found = self.store.find(self.run_id)
            if found is not None and found.state is RunState.TERMINAL:
                break
            time.sleep(0.2)
        found = self.store.find(self.run_id)
        self.assertIsNotNone(found, msg="stored")
        self.assertEqual(found.terminal_reason if found else None, TerminalReason.COMPLETED)


if __name__ == "__main__":
    unittest.main()
