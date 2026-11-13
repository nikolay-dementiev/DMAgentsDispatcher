"""Tests of the per-run owner lock: one live supervisor per run, visible to every other process."""

import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from agents_dispatcher.adapters.system.run_owner_lock import FileRunOwners, OwnerLockError
from agents_dispatcher.domain.run import RunId
from tests.support.hard_timeout import HardTimeoutCase

HOLDER = """
import sys
from agents_dispatcher.adapters.system.run_owner_lock import FileRunOwners
from agents_dispatcher.domain.run import RunId
owners = FileRunOwners(sys.argv[1])
owners.hold(RunId(sys.argv[2]))
print('held', flush=True)
sys.stdin.read()
"""


class RunOwnerLockTests(HardTimeoutCase):
    """What a status read or a dead-owner check can learn from the lock files."""

    def setUp(self) -> None:
        """Create a control folder and a run id."""
        super().setUp()
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.control = str(Path(scratch.name) / "control")
        self.run_id = RunId(str(uuid.uuid4()))
        self.owners = FileRunOwners(self.control)
        self.children: list[subprocess.Popen[str]] = []
        self.addCleanup(self.reap)

    def reap(self) -> None:
        """Kill and reap every holder process."""
        for child in self.children:
            child.kill()
            child.wait(timeout=10)
            for stream in (child.stdin, child.stdout):
                if stream is not None:
                    stream.close()

    def holder(self) -> subprocess.Popen[str]:
        """Start a process that holds the lock of this run until it dies."""
        child = subprocess.Popen(  # noqa: S603 - argv array of this interpreter, no shell
            [sys.executable, "-B", "-c", HOLDER, self.control, self.run_id.value],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.children.append(child)
        line = child.stdout.readline() if child.stdout else ""
        self.assertEqual(line.strip(), "held", msg="the holder took the lock")
        return child

    def test_a_run_nobody_holds_has_no_live_owner(self) -> None:
        """No lock file is the same as no owner, and asking creates nothing."""
        self.assertFalse(self.owners.owner_alive(self.run_id), msg="no owner")
        self.assertFalse(Path(self.control).exists(), msg="a probe leaves no trace")

    def test_a_held_lock_is_seen_by_another_process_and_released_with_it(self) -> None:
        """The kernel drops the lock when the holder dies, so a crash never blocks recovery."""
        child = self.holder()
        self.assertTrue(self.owners.owner_alive(self.run_id), msg="alive while held")
        child.kill()
        child.wait(timeout=10)
        deadline = time.monotonic() + 10.0
        while self.owners.owner_alive(self.run_id) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(self.owners.owner_alive(self.run_id), msg="dead after the holder died")

    def test_only_one_owner_per_run_and_a_release_hands_the_run_back(self) -> None:
        """A second holder is refused with an error; after the release it may take over."""
        first = self.owners.hold(self.run_id)
        with self.assertRaises(OwnerLockError, msg="second owner refused"):
            FileRunOwners(self.control).hold(self.run_id)
        self.assertTrue(self.owners.owner_alive(self.run_id), msg="alive while held")
        first.release()
        self.assertFalse(self.owners.owner_alive(self.run_id), msg="released")
        FileRunOwners(self.control).hold(self.run_id).release()

    def test_another_run_is_not_affected(self) -> None:
        """Locks are per run."""
        self.owners.hold(self.run_id)
        self.assertFalse(self.owners.owner_alive(RunId(str(uuid.uuid4()))), msg="other run")

    def test_the_files_are_private_to_the_owner(self) -> None:
        """Control folder 0700 and lock file 0600."""
        self.owners.hold(self.run_id).release()
        folder = stat.S_IMODE(Path(self.control).stat().st_mode)
        lock = stat.S_IMODE(
            (Path(self.control) / "locks" / f"{self.run_id.value}.lock").stat().st_mode
        )
        self.assertEqual(folder, 0o700, msg="control folder mode")
        self.assertEqual(lock, 0o600, msg="lock file mode")

    def test_loose_folders_from_an_earlier_version_are_tightened(self) -> None:
        """A folder that exists with wider permissions is made private before a lock is taken."""
        locks = Path(self.control) / "locks"
        previous = os.umask(0)
        try:
            locks.mkdir(parents=True, mode=0o755)
        finally:
            os.umask(previous)
        self.owners.hold(self.run_id).release()
        self.assertEqual(stat.S_IMODE(locks.stat().st_mode), 0o700, msg="locks folder")
        self.assertEqual(stat.S_IMODE(Path(self.control).stat().st_mode), 0o700, msg="control")


if __name__ == "__main__":
    unittest.main()
