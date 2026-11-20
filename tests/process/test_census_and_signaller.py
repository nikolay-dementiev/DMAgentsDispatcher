"""Tests of the real census and signaller against real child processes."""

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from agents_dispatcher.adapters.system.os_signaller import OsSignaller
from agents_dispatcher.adapters.system.process_census import LibprocCensus

from agents_dispatcher.adapters.system.process_table import LibprocProcessTable
from agents_dispatcher.application.ports import SignalResult
from agents_dispatcher.domain.process import Absence, ProcessBasics, ProcessKey
from tests.process.test_process_table import READY, spawn_python, stop
from tests.support.hard_timeout import HardTimeoutCase

MARKER_ENV = "AGENTSD_RUN_ID"
WAIT_S = 10.0
POLL_S = 0.02
GONE_PID = 4_194_000


class RealWorldCase(HardTimeoutCase):
    """A table, a census, a signaller and a private folder for each test."""

    def setUp(self) -> None:
        """Create the adapters under test and a folder that is cleaned up."""
        super().setUp()
        self.table = LibprocProcessTable()
        self.census = LibprocCensus(self.table)
        self.signaller = OsSignaller(self.table)
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.real = Path(self.folder.name).resolve()

    def child(self, code: str, *, env: dict[str, str] | None = None) -> subprocess.Popen[str]:
        """Start a ready child and clean it up after the test."""
        started = spawn_python(code, env=env)
        self.addCleanup(stop, started)
        return started

    def key_of(self, child: subprocess.Popen[str]) -> ProcessKey:
        """Return the identity of a live child."""
        basics = self.table.basics(child.pid)
        assert isinstance(basics, ProcessBasics)  # noqa: S101 - narrows the type for mypy
        return ProcessKey(basics.pid, basics.start_time)

    def zombie(self) -> subprocess.Popen[bytes]:
        """Start a child that exits at once and is not reaped until the test ends."""
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        self.addCleanup(child.wait)
        deadline = time.monotonic() + WAIT_S
        while time.monotonic() < deadline:
            basics = self.table.basics(child.pid)
            if isinstance(basics, ProcessBasics) and basics.zombie:
                return child
            time.sleep(POLL_S)
        message = "the child did not become a zombie"
        raise AssertionError(message)


class CensusTests(RealWorldCase):
    """A look at the table: the marker, the paths, the zombies, the completeness."""

    def test_a_marked_child_is_listed_with_marker_and_identity(self) -> None:
        """The marker comes from the environment at exec, the identity from the table."""
        child = self.child(
            f"print({READY!r}, flush=True); input()", env={**os.environ, MARKER_ENV: "m-1"}
        )
        rows = {row.key.pid: row for row in self.census.look(paths=False).rows}
        row = rows[child.pid]
        self.assertEqual(row.marker, "m-1", "marker read")
        self.assertEqual(row.key, self.key_of(child), "same identity as the table gives")
        self.assertEqual(row.ppid, os.getpid(), "parent")

    def test_the_paths_are_read_only_when_asked(self) -> None:
        """A cheap look leaves them out; the full look has the cwd and an open file."""
        held = self.real / "held.txt"
        held.write_text("x", encoding="utf-8")
        child = self.child(f"handle = open({str(held)!r}); print({READY!r}, flush=True); input()")
        cheap = {row.key.pid: row for row in self.census.look(paths=False).rows}[child.pid]
        full = {row.key.pid: row for row in self.census.look(paths=True).rows}[child.pid]
        self.assertEqual((cheap.cwd, cheap.open_paths), (None, ()), "no paths in the cheap look")
        self.assertIn(str(held), full.open_paths, "the open file in the full look")
        self.assertEqual(full.cwd, str(Path.cwd().resolve()), "the cwd in the full look")

    def test_a_complete_look_is_reported_complete(self) -> None:
        """Nothing was unreadable: the look may count as empty evidence."""
        self.assertTrue(self.census.look(paths=True).complete, "complete")

    def test_a_zombie_is_not_listed(self) -> None:
        """An ended process that was not reaped holds nothing and cannot be signalled."""
        zombie = self.zombie()
        pids = {row.key.pid for row in self.census.look(paths=False).rows}
        self.assertNotIn(zombie.pid, pids, "a zombie is not alive")

    def test_the_footprint_of_a_child_is_read(self) -> None:
        """The memory of a listed process is available for the watch."""
        child = self.child(f"print({READY!r}, flush=True); input()")
        self.assertGreater(self.census.footprint(child.pid) or 0, 0, "footprint")


class SignallerTests(RealWorldCase):
    """Signals that revalidate the identity first."""

    @staticmethod
    def stopped_now(child: subprocess.Popen[str]) -> bool:
        """Return whether the child is stopped, reading its state without blocking."""
        pid, status = os.waitpid(child.pid, os.WUNTRACED | os.WNOHANG)
        return pid == child.pid and os.WIFSTOPPED(status)

    def test_stop_pauses_and_kill_ends_the_named_process(self) -> None:
        """The right pid with the right start time is signalled."""
        child = self.child(f"print({READY!r}, flush=True); input()")
        key = self.key_of(child)
        self.assertIs(self.signaller.stop(key), SignalResult.SENT, "stop")
        deadline = time.monotonic() + WAIT_S
        while not self.stopped_now(child) and time.monotonic() < deadline:
            time.sleep(POLL_S)
        self.assertIs(self.signaller.kill(key), SignalResult.SENT, "kill")
        self.assertEqual(child.wait(timeout=WAIT_S), -9, "ended by SIGKILL")

    def test_a_pid_with_another_start_time_is_left_alone(self) -> None:
        """A reused pid is another process: nothing is sent."""
        child = self.child(f"print({READY!r}, flush=True); input()")
        key = self.key_of(child)
        stranger = ProcessKey(key.pid, key.start_time + 100.0)
        self.assertIs(self.signaller.kill(stranger), SignalResult.CHANGED, "changed")
        self.assertIsNone(child.poll(), "the child still runs")

    def test_a_pid_nobody_holds_is_gone(self) -> None:
        """No process, no signal."""
        self.assertIs(self.signaller.kill(ProcessKey(GONE_PID, 1.0)), SignalResult.GONE, "gone")

    def test_a_zombie_is_gone(self) -> None:
        """Signalling a zombie is refused by the system; it is already dead for our purposes."""
        zombie = self.zombie()
        self.assertIs(self.signaller.kill(ProcessKey(zombie.pid, 0.0)), SignalResult.GONE, "gone")

    def test_a_process_of_another_user_is_denied(self) -> None:
        """A process that cannot even be read is not signalled."""
        if os.geteuid() == 0:
            self.skipTest("root can read every process")
        foreign = [
            pid
            for pid in self.table.all_pids()
            if pid > 1 and self.table.basics(pid) is Absence.DENIED
        ]
        if not foreign:
            self.skipTest("no process of another user is visible")
        self.assertIs(
            self.signaller.kill(ProcessKey(foreign[0], 1.0)), SignalResult.DENIED, "denied"
        )

    def test_pid_zero_and_one_are_refused_up_front(self) -> None:
        """Pid 0 would signal our own group and pid 1 is launchd, whoever runs the test."""
        for pid in (0, 1):
            with self.subTest(pid=pid):
                result = self.signaller.kill(ProcessKey(pid, 1.0))
                self.assertIs(result, SignalResult.DENIED, f"pid {pid}")


if __name__ == "__main__":
    unittest.main()
