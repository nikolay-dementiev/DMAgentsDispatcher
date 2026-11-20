"""Tests of what a census look may call complete, over a scripted table."""

import signal
import unittest

from agents_dispatcher.adapters.system.os_signaller import OsSignaller
from agents_dispatcher.adapters.system.process_census import LibprocCensus
from agents_dispatcher.application.ports import SignalResult
from agents_dispatcher.domain.process import Absence, ProcessBasics, ProcessKey

MARKER_ENV = "AGENTSD_RUN_ID"


class ScriptedTable:
    """A table whose answers are set per pid by the test."""

    def __init__(self) -> None:
        """Start with nothing listed."""
        self.pids: list[int] | None = []
        self.environments: dict[int, dict[str, str] | None] = {}
        self.cwds: dict[int, str | Absence] = {}
        self.files: dict[int, tuple[str, ...] | Absence] = {}
        self.vanished_after_environment: set[int] = set()
        self._environment_reads: set[int] = set()

    def listing(self) -> list[int] | None:
        """Return the scripted pids."""
        return self.pids

    def basics(self, pid: int) -> ProcessBasics | Absence:
        """Answer GONE for a process that vanished after its environment was tried."""
        if pid in self.vanished_after_environment and pid in self._environment_reads:
            return Absence.GONE
        if pid not in self.environments:
            return Absence.DENIED
        return ProcessBasics(pid=pid, ppid=1, pgid=pid, start_time=1000.0, descriptor_count=3)

    def environment(self, pid: int) -> dict[str, str] | None:
        """Return the scripted environment."""
        self._environment_reads.add(pid)
        return self.environments.get(pid)

    def cwd(self, pid: int) -> str | Absence:
        """Return the scripted working directory."""
        return self.cwds.get(pid, "/")

    def open_paths(self, pid: int) -> tuple[str, ...] | Absence:
        """Return the scripted open files."""
        return self.files.get(pid, ())

    def footprint(self, pid: int) -> int | None:  # noqa: ARG002 - the scripted table has none
        """Return nothing: memory is not part of these tests."""
        return None


class CompletenessTests(unittest.TestCase):
    """When a look counts as evidence that nothing is left."""

    def setUp(self) -> None:
        """Create the scripted table and the census over it."""
        super().setUp()
        self.table = ScriptedTable()
        self.census = LibprocCensus(self.table)

    def test_a_table_that_could_not_be_listed_is_incomplete(self) -> None:
        """No list of pids, no evidence."""
        self.table.pids = None
        snapshot = self.census.look(paths=True)
        self.assertFalse(snapshot.complete, "incomplete")
        self.assertEqual(snapshot.rows, (), "and empty")

    def test_a_process_of_another_user_is_outside_the_picture_and_harmless(self) -> None:
        """Its reads are refused for good; that must not make every look incomplete."""
        self.table.pids = [500]
        snapshot = self.census.look(paths=True)
        self.assertTrue(snapshot.complete, "still complete")
        self.assertEqual(snapshot.rows, (), "and not listed")

    def test_a_live_process_whose_environment_cannot_be_read_is_incomplete(self) -> None:
        """Its marker is unknown, so it might belong to the run."""
        self.table.pids = [600]
        self.table.environments[600] = None
        self.assertFalse(self.census.look(paths=False).complete, "incomplete")

    def test_a_process_that_ended_while_being_read_is_not_a_gap(self) -> None:
        """Its environment failed because it was gone: nothing is hidden."""
        self.table.pids = [600]
        self.table.environments[600] = None
        self.table.vanished_after_environment.add(600)
        snapshot = self.census.look(paths=False)
        self.assertTrue(snapshot.complete, "complete")
        self.assertEqual(snapshot.rows, (), "and not listed")

    def test_a_system_daemon_that_refuses_its_open_files_keeps_the_look_complete(self) -> None:
        """Seen on a real machine: protected daemons of this user refuse the descriptor listing."""
        self.table.pids = [700]
        self.table.environments[700] = {}
        self.table.files[700] = Absence.DENIED
        snapshot = self.census.look(paths=True)
        self.assertTrue(snapshot.complete, "complete")
        self.assertEqual([row.key.pid for row in snapshot.rows], [700], "listed")
        self.assertEqual(snapshot.rows[0].open_paths, (), "without open files")

    def test_a_process_that_ended_before_its_paths_were_read_is_not_a_gap(self) -> None:
        """GONE on a path read means it left between two reads."""
        self.table.pids = [800]
        self.table.environments[800] = {MARKER_ENV: "m-1"}
        self.table.cwds[800] = Absence.GONE
        snapshot = self.census.look(paths=True)
        self.assertTrue(snapshot.complete, "complete")
        self.assertEqual(snapshot.rows, (), "and not listed")


class ReadableEverywhere:
    """A table that shows every pid as a live process started at 1000.0."""

    def basics(self, pid: int) -> ProcessBasics | Absence:
        """Return the same start time for every pid."""
        return ProcessBasics(pid=pid, ppid=1, pgid=pid, start_time=1000.0, descriptor_count=3)


class SignalGuardTests(unittest.TestCase):
    """What the signaller refuses before any signal is delivered."""

    def test_pid_zero_and_one_are_never_signalled_even_when_the_table_shows_them(self) -> None:
        """Pid 0 is our own group and pid 1 is launchd; root could otherwise reach them."""
        sent: list[tuple[int, int]] = []
        signaller = OsSignaller(
            ReadableEverywhere(), lambda pid, number: sent.append((pid, number))
        )
        for pid in (0, 1):
            result = signaller.kill(ProcessKey(pid, 1000.0))
            self.assertIs(result, SignalResult.DENIED, f"pid {pid} is refused")
        self.assertEqual(sent, [], "nothing was delivered")

    def test_a_matching_identity_is_delivered_exactly_once(self) -> None:
        """The signal goes to the pid that was named, and only then."""
        sent: list[tuple[int, int]] = []
        signaller = OsSignaller(
            ReadableEverywhere(), lambda pid, number: sent.append((pid, number))
        )
        self.assertIs(signaller.kill(ProcessKey(4321, 1000.0)), SignalResult.SENT, "sent")
        self.assertEqual(sent, [(4321, signal.SIGKILL)], "one delivery to that pid")


if __name__ == "__main__":
    unittest.main()
