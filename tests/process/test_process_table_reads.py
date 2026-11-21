"""Tests of the three-valued process reads: parent, descriptors, cwd and open files."""

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from agents_dispatcher.adapters.system.process_table import LibprocProcessTable
from agents_dispatcher.domain.process import Absence, ProcessBasics
from tests.process.test_process_table import READY, spawn_python, stop
from tests.support.hard_timeout import HardTimeoutCase

GONE_PID = 4_194_000
HELD_FILES = 7
STD_STREAMS = 3


class ProcessReadTests(HardTimeoutCase):
    """The table says what a process is, or why it cannot say."""

    def setUp(self) -> None:
        """Create the table and a private folder that is cleaned up after the test."""
        super().setUp()
        self.table = LibprocProcessTable()
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.real = Path(self.folder.name).resolve()

    def child(self, code: str) -> subprocess.Popen[str]:
        """Start a ready child and clean it up after the test."""
        started = spawn_python(code)
        self.addCleanup(stop, started)
        return started

    def test_basics_of_a_child_name_its_parent_group_and_start(self) -> None:
        """The parent is this process, the child leads its own group, the start is read."""
        child = self.child(f"print({READY!r}, flush=True); input()")
        basics = self.table.basics(child.pid)
        assert isinstance(basics, ProcessBasics)  # noqa: S101 - narrows the type for mypy
        self.assertEqual(basics.ppid, os.getpid(), "the parent pid is this process")
        self.assertEqual(basics.pgid, child.pid, "the child was started as a group leader")
        self.assertGreater(basics.start_time, 0.0, "the start time is a wall-clock time")

    def test_basics_count_open_descriptors(self) -> None:
        """The descriptor count follows the files a child holds open."""
        code = f"""
            files = [open('/dev/null') for _ in range({HELD_FILES})]
            print({READY!r}, flush=True); input()
        """
        child = self.child(code)
        basics = self.table.basics(child.pid)
        assert isinstance(basics, ProcessBasics)  # noqa: S101 - narrows the type for mypy
        self.assertGreaterEqual(
            basics.descriptor_count, HELD_FILES + STD_STREAMS, "the held files and the std streams"
        )

    def test_cwd_is_the_real_path_of_the_working_directory(self) -> None:
        """A child started in a folder reports that folder's real path."""
        code = f"print({READY!r}, flush=True); input()"
        child = subprocess.Popen(  # noqa: S603 - argv array of this interpreter, no shell
            [sys.executable, "-c", textwrap.dedent(code)],
            stdout=subprocess.PIPE,
            text=True,
            cwd=self.real,
        )
        self.addCleanup(stop, child)
        assert child.stdout is not None  # noqa: S101 - narrows the type for mypy
        self.assertEqual(child.stdout.readline().strip(), READY, "the child is ready")
        self.assertEqual(self.table.cwd(child.pid), str(self.real), "the cwd is the folder")

    def test_open_paths_list_a_file_the_child_holds(self) -> None:
        """A regular file held open is listed with its real path."""
        held = self.real / "held.txt"
        held.write_text("x", encoding="utf-8")
        code = f"""
            handle = open({str(held)!r})
            print({READY!r}, flush=True); input()
        """
        child = self.child(code)
        paths = self.table.open_paths(child.pid)
        assert not isinstance(paths, Absence)  # noqa: S101 - narrows the type for mypy
        self.assertIn(str(held), paths, "the held file is listed by real path")

    def test_a_process_of_another_user_is_denied_not_gone(self) -> None:
        """Pid 1 belongs to root: every read says DENIED, never GONE."""
        if os.geteuid() == 0:
            self.skipTest("root can read every process")
        self.assertIs(self.table.basics(1), Absence.DENIED, "basics of pid 1")
        self.assertIs(self.table.cwd(1), Absence.DENIED, "cwd of pid 1")
        self.assertIs(self.table.open_paths(1), Absence.DENIED, "open files of pid 1")

    def test_a_pid_that_does_not_exist_is_gone(self) -> None:
        """A pid nobody holds is GONE on every read."""
        self.assertIs(self.table.basics(GONE_PID), Absence.GONE, "basics of a missing pid")
        self.assertIs(self.table.cwd(GONE_PID), Absence.GONE, "cwd of a missing pid")
        self.assertIs(self.table.open_paths(GONE_PID), Absence.GONE, "open files of a missing pid")


if __name__ == "__main__":
    unittest.main()
