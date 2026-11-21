"""Tests of the wait for the supervisors of a temporary account."""

import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.support.supervisors import wait_for_supervisors


class WaitTests(unittest.TestCase):
    """A store file with supervisor pids and the processes behind them."""

    def setUp(self) -> None:
        """Create a store file with the one column the wait reads."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = Path(directory.name) / "runs.sqlite3"
        with sqlite3.connect(self.store) as connection:
            connection.execute("CREATE TABLE runs (run_id TEXT, supervisor_pid INTEGER)")

    def record(self, pid: int | None) -> None:
        """Record a run with this supervisor pid."""
        with sqlite3.connect(self.store) as connection:
            connection.execute("INSERT INTO runs VALUES ('r', ?)", (pid,))

    def test_it_returns_once_the_supervisor_has_exited(self) -> None:
        """A process that is still ending is waited for, not abandoned."""
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(0.4)"])
        self.addCleanup(child.wait)
        self.record(child.pid)
        self.assertEqual(wait_for_supervisors(self.store, timeout_s=10.0), [], msg="all gone")
        self.assertIsNotNone(child.poll(), msg="and it really ended")

    def test_a_supervisor_that_stays_alive_is_reported_after_the_timeout(self) -> None:
        """The wait is bounded and names what it could not wait out."""
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        self.record(child.pid)
        self.assertEqual(
            wait_for_supervisors(self.store, timeout_s=0.3), [child.pid], msg="still alive"
        )

    def test_a_stand_in_pid_of_a_fixture_is_not_waited_for(self) -> None:
        """A fixture records pid 1 for a supervisor that never existed; it never exits."""
        self.record(1)
        self.assertEqual(wait_for_supervisors(self.store, timeout_s=0.2), [], msg="ignored")

    def test_a_store_that_does_not_exist_has_nothing_to_wait_for(self) -> None:
        """A test that never started a run leaves no store."""
        self.assertEqual(
            wait_for_supervisors(self.store.parent / "none.sqlite3", timeout_s=0.1),
            [],
            msg="nothing",
        )


if __name__ == "__main__":
    unittest.main()
