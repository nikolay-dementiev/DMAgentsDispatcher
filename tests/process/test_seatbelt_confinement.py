"""Tests of the Seatbelt confinement with real sandbox-exec, real children and real signals.

Every claim is judged by the file system, the network and the process table, never by what the
worker says about itself (digest section 2).
"""

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from agents_dispatcher.adapters.system.seatbelt_confinement import (
    ConfinementConfig,
    SeatbeltConfinement,
)

from agents_dispatcher.adapters.system.process_table import LibprocProcessTable
from agents_dispatcher.adapters.system.system_clock import SuspendInclusiveClock
from agents_dispatcher.application.errors import LaunchError
from agents_dispatcher.application.ports import (
    EgressEndpoint,
    ExecutionOutcome,
    PreparedWorkspace,
    ProcessIdentity,
    WorkerCommand,
)
from agents_dispatcher.domain.run import LimitKind, Limits, RunId
from tests.support.hard_timeout import HardTimeoutCase
from tests.support.narrow import present

RUN_ID = RunId("0a1b2c3d-4e5f-4a6b-8c7d-9e0f1a2b3c4d")
BIG = Limits(hard_s=60.0, memory_bytes=8 * 1024**3)
TERM_GRACE_S = 0.5
KILL_WAIT_S = 1.0
MIB = 1024 * 1024


def alive(pid: int) -> bool:
    """Return whether a process with this pid exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class ConfinementCase(HardTimeoutCase):
    """A sandbox directory, a directory outside it, a listener standing in for the proxy."""

    def setUp(self) -> None:
        """Create the directories and the fake proxy port."""
        super().setUp()
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name).resolve()
        self.sandbox = root / RUN_ID.sandbox_name
        self.worktree = self.sandbox / "worktree"
        self.worktree.mkdir(parents=True)
        self.outside = root / "outside"
        self.outside.mkdir()
        self.secret = root / "secret"
        self.secret.mkdir()
        (self.secret / "token.txt").write_text("hunter2", encoding="utf-8")
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen()
        self.addCleanup(self.listener.close)
        self.other_listener = socket.socket()
        self.other_listener.bind(("127.0.0.1", 0))
        self.other_listener.listen()
        self.addCleanup(self.other_listener.close)
        self.proxy_port = self.listener.getsockname()[1]
        self.other_port = self.other_listener.getsockname()[1]
        self.table = LibprocProcessTable()
        self.started: list[ProcessIdentity] = []

    def confinement(self, *, denied: tuple[str, ...] = ()) -> SeatbeltConfinement:
        """Build the confinement under test."""
        return SeatbeltConfinement(
            ConfinementConfig(
                denied_reads=denied,
                term_grace_s=TERM_GRACE_S,
                kill_wait_s=KILL_WAIT_S,
                scan_s=0.1,
                interpreter=sys.executable,
            ),
            table=self.table,
            clock=SuspendInclusiveClock(),
        )

    def execute(
        self,
        code: str,
        *,
        limits: Limits = BIG,
        denied: tuple[str, ...] = (),
        argv: tuple[str, ...] | None = None,
    ) -> ExecutionOutcome:
        """Run Python `code` (or `argv`) as the worker and return the outcome."""
        workspace = PreparedWorkspace(
            base_commit="b" * 40,
            run_id=RUN_ID,
            sandbox_dir=str(self.sandbox),
            worktree_dir=str(self.worktree),
            branch_name=RUN_ID.branch_name,
        )
        command = WorkerCommand(
            argv=argv or (sys.executable, "-B", "-c", textwrap.dedent(code)),
            environment={"PATH": "/usr/bin:/bin", "HOME": str(self.outside)},
            stdin="the brief",
            cwd=str(self.worktree),
        )
        egress = EgressEndpoint(host="127.0.0.1", port=self.proxy_port)
        return self.confinement(denied=denied).run(
            command, workspace, egress, limits, self.started.append
        )


class WriteAndReadTests(ConfinementCase):
    """File-system confinement."""

    def test_writes_are_allowed_in_the_sandbox_and_denied_everywhere_else(self) -> None:
        """Judged by the files that exist afterwards."""
        outcome = self.execute(
            f"""
            import pathlib
            for target in ({str(self.worktree / "in.txt")!r}, {str(self.outside / "out.txt")!r}):
                try:
                    pathlib.Path(target).write_text("x")
                except OSError:
                    pass
            """
        )
        self.assertEqual(outcome.exit_status, 0, msg="the worker ran")
        self.assertTrue((self.worktree / "in.txt").exists(), msg="sandbox write allowed")
        self.assertFalse((self.outside / "out.txt").exists(), msg="outside write denied")

    def test_denied_read_areas_cannot_be_read_but_the_sandbox_can(self) -> None:
        """The read-deny inventory is enforced."""
        (self.sandbox / "note.txt").write_text("fine", encoding="utf-8")
        outcome = self.execute(
            f"""
            import json, pathlib
            result = {{}}
            for name, target in (("secret", {str(self.secret / "token.txt")!r}),
                                 ("note", {str(self.sandbox / "note.txt")!r})):
                try:
                    result[name] = pathlib.Path(target).read_text()
                except OSError:
                    result[name] = "denied"
            print(json.dumps(result))
            """,
            denied=(str(self.secret),),
        )
        self.assertEqual(
            json.loads(outcome.stdout),
            {"secret": "denied", "note": "fine"},
            msg="secret denied, sandbox readable",
        )

    def test_the_brief_arrives_on_standard_input_and_output_is_captured(self) -> None:
        """Standard streams are files in the sandbox, not shell strings."""
        outcome = self.execute("import sys; print(sys.stdin.read().upper())")
        self.assertEqual(outcome.stdout.strip(), "THE BRIEF", msg="stdin to stdout")
        self.assertEqual(outcome.exit_status, 0, msg="exit status")


class NetworkTests(ConfinementCase):
    """Only the proxy port on loopback is reachable."""

    def test_only_the_proxy_port_is_reachable(self) -> None:
        """A connection to another loopback port fails; the proxy port works."""
        outcome = self.execute(
            f"""
            import json, socket
            result = {{}}
            for name, port in (("proxy", {self.proxy_port}), ("other", {self.other_port})):
                try:
                    socket.create_connection(("127.0.0.1", port), timeout=3).close()
                    result[name] = "open"
                except OSError:
                    result[name] = "refused"
            print(json.dumps(result))
            """
        )
        self.assertEqual(
            json.loads(outcome.stdout), {"proxy": "open", "other": "refused"}, msg="ports"
        )


class LimitTests(ConfinementCase):
    """Time and memory limits end the run and name the limit."""

    def test_the_hard_time_limit_stops_the_worker(self) -> None:
        """A sleeper is stopped after hard_s and the outcome names the limit."""
        started = time.monotonic()
        outcome = self.execute("import time; time.sleep(120)", limits=Limits(1.0, 8 * 1024**3))
        self.assertEqual(outcome.limit_hit, LimitKind.TIME, msg="time limit named")
        self.assertLess(time.monotonic() - started, 15.0, msg="stopped promptly")
        self.assertEqual(outcome.survivors_alive, (), msg="nothing left alive")

    def test_the_memory_limit_stops_the_worker(self) -> None:
        """Dirty memory above the limit is caught through the footprint."""
        outcome = self.execute(
            """
            import time
            data = bytearray(400 * 1024 * 1024)
            for index in range(0, len(data), 4096):
                data[index] = 1
            time.sleep(120)
            """,
            limits=Limits(hard_s=60.0, memory_bytes=150 * MIB),
        )
        self.assertEqual(outcome.limit_hit, LimitKind.MEMORY, msg="memory limit named")

    def test_memory_of_a_process_that_left_the_group_still_counts(self) -> None:
        """F4: the sum covers what the marker sweep finds, not only the group."""
        outcome = self.execute(
            """
            import subprocess, sys, time
            code = ("import time\\nd = bytearray(400*1024*1024)\\n"
                    "for i in range(0, len(d), 4096): d[i] = 1\\ntime.sleep(120)")
            subprocess.Popen([sys.executable, "-B", "-c", code], start_new_session=True)
            time.sleep(120)
            """,
            limits=Limits(hard_s=60.0, memory_bytes=150 * MIB),
        )
        self.assertEqual(outcome.limit_hit, LimitKind.MEMORY, msg="escaped memory counted")
        self.assertEqual(outcome.survivors_alive, (), msg="the escaped process was stopped")


class CleanupTests(ConfinementCase):
    """Nothing the worker started outlives the run."""

    def test_a_term_ignoring_background_child_is_gone_after_the_run(self) -> None:
        """Group stop reaches the whole group."""
        pid_file = self.sandbox / "child.pid"
        outcome = self.execute(
            f"""
            import pathlib, subprocess, sys
            child = subprocess.Popen([sys.executable, "-B", "-c",
                "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                "time.sleep(120)"])
            pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))
            """
        )
        child = int(pid_file.read_text(encoding="utf-8"))
        self.assertEqual(outcome.exit_status, 0, msg="the worker itself finished")
        self.assertFalse(alive(child), msg="the background child is gone")
        self.assertEqual(outcome.survivors_alive, (), msg="no survivor reported alive")

    def test_a_setsid_descendant_with_the_marker_is_found_and_killed(self) -> None:
        """The sweep finds what left the group, kills it, and reports it."""
        pid_file = self.sandbox / "escaped.pid"
        outcome = self.execute(
            f"""
            import pathlib, subprocess, sys
            child = subprocess.Popen([sys.executable, "-B", "-c", "import time; time.sleep(120)"],
                                     start_new_session=True)
            pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))
            """
        )
        escaped = int(pid_file.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 5.0
        while alive(escaped) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(alive(escaped), msg="the escaped process was killed")
        self.assertTrue(
            any(str(escaped) in entry for entry in outcome.survivors), msg="and it was reported"
        )

    def test_the_identity_of_the_group_leader_is_reported_at_start(self) -> None:
        """The store gets the group and the leader's start time for the start-up sweep."""
        self.execute("pass")
        identity = present(self.started[0] if self.started else None)
        self.assertGreater(identity.pgid, 1, msg="a real group")
        self.assertGreater(identity.start_time_s, 1_000_000_000, msg="a wall-clock start time")

    def test_a_missing_worker_executable_is_a_launch_error(self) -> None:
        """The run never started; the caller maps this to LAUNCH_LOST."""
        with self.assertRaises(LaunchError):
            self.execute("", argv=("/nonexistent/worker",))


class DeadParentTests(ConfinementCase):
    """SIGKILL of the supervisor must not leave the worker behind."""

    def test_killing_the_supervisor_ends_the_worker_group(self) -> None:
        """The shim sees end-of-file on the control pipe and stops the group."""
        pid_file = self.sandbox / "worker.pid"
        supervisor_code = textwrap.dedent(
            f"""
            import sys
            sys.path[:0] = {sys.path!r}
            from agents_dispatcher.adapters.system.process_table import LibprocProcessTable
            from agents_dispatcher.adapters.system.seatbelt_confinement import (
                ConfinementConfig, SeatbeltConfinement)
            from agents_dispatcher.adapters.system.system_clock import SuspendInclusiveClock
            from agents_dispatcher.application.ports import (
                EgressEndpoint, PreparedWorkspace, WorkerCommand)
            from agents_dispatcher.domain.run import Limits, RunId
            run_id = RunId({RUN_ID.value!r})
            workspace = PreparedWorkspace(base_commit="b" * 40, run_id=run_id,
                sandbox_dir={str(self.sandbox)!r}, worktree_dir={str(self.worktree)!r},
                branch_name=run_id.branch_name)
            worker_code = (
                "import os, pathlib, time; "
                "pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); "
                "time.sleep(300)"
            )
            command = WorkerCommand(
                argv=(sys.executable, "-B", "-c", worker_code),
                environment={{"PATH": "/usr/bin:/bin"}}, stdin="", cwd={str(self.worktree)!r})
            config = ConfinementConfig(denied_reads=(), term_grace_s={TERM_GRACE_S},
                kill_wait_s={KILL_WAIT_S}, scan_s=0.1, interpreter=sys.executable)
            SeatbeltConfinement(config, table=LibprocProcessTable(),
                clock=SuspendInclusiveClock()).run(
                command, workspace, EgressEndpoint("127.0.0.1", {self.proxy_port}),
                Limits(300.0, 8 * 1024**3), lambda identity: None)
            """
        )
        supervisor = subprocess.Popen(  # noqa: S603 - argv array of this interpreter, no shell
            [sys.executable, "-B", "-c", supervisor_code], stderr=subprocess.PIPE
        )
        self.addCleanup(supervisor.wait)
        self.addCleanup(present(supervisor.stderr).close)
        deadline = time.monotonic() + 30.0
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        worker = int(pid_file.read_text(encoding="utf-8"))
        self.assertTrue(alive(worker), msg="the worker runs while the supervisor lives")
        supervisor.send_signal(signal.SIGKILL)
        deadline = time.monotonic() + TERM_GRACE_S + KILL_WAIT_S + 10.0
        while alive(worker) and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertFalse(alive(worker), msg="the worker is gone after the supervisor was killed")


if __name__ == "__main__":
    unittest.main()
