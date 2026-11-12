"""Tests of the libproc and sysctl process table against real child processes."""

import os
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

from agents_dispatcher.adapters.system.process_table import LibprocProcessTable
from tests.support.narrow import present

MIB = 1024 * 1024
READY = "ready"


def spawn_python(code: str, *, env: dict[str, str] | None = None) -> subprocess.Popen[str]:
    """Start a Python child in its own session, wait until it prints READY, and return it."""
    child = subprocess.Popen(  # noqa: S603 - argv array of this interpreter, no shell
        [sys.executable, "-c", textwrap.dedent(code)],
        stdout=subprocess.PIPE,
        text=True,
        start_new_session=True,
        env=env,
    )
    line = present(child.stdout).readline().strip()
    if line != READY:
        child.kill()
        message = f"child did not become ready: {line!r}"
        raise AssertionError(message)
    return child


def stop(child: subprocess.Popen[str]) -> None:
    """Kill and reap a test child."""
    child.kill()
    child.wait(timeout=10)
    if child.stdout:
        child.stdout.close()


class ProcessTableTests(unittest.TestCase):
    """Group membership, start time, memory footprint and the environment marker."""

    def setUp(self) -> None:
        """Create the table under test."""
        self.table = LibprocProcessTable()

    def child(self, code: str, *, env: dict[str, str] | None = None) -> subprocess.Popen[str]:
        """Start a ready child and clean it up after the test."""
        started = spawn_python(code, env=env)
        self.addCleanup(stop, started)
        return started

    def test_all_pids_lists_this_process(self) -> None:
        """The enumeration sees the running test process."""
        self.assertIn(os.getpid(), self.table.all_pids(), msg="own pid is listed")

    def test_group_members_lists_the_leader_and_its_descendants_in_the_group(self) -> None:
        """A grandchild that stays in the group is a member; the test process is not."""
        child = self.child(
            f"""
            import subprocess
            grandchild = subprocess.Popen(["/bin/sleep", "60"])
            print({READY!r}, flush=True)
            print(grandchild.pid, flush=True)
            grandchild.wait()
            """
        )
        grandchild_pid = int(present(child.stdout).readline())
        members = self.table.group_members(child.pid)
        self.assertIn(child.pid, members, msg="the leader is a member")
        self.assertIn(grandchild_pid, members, msg="the grandchild is a member")
        self.assertNotIn(os.getpid(), members, msg="the test process is not a member")

    def test_group_members_of_an_empty_group_is_empty(self) -> None:
        """After the leader is gone and reaped, nothing is left."""
        child = spawn_python(f"print({READY!r}, flush=True)")
        stop(child)
        self.assertEqual(self.table.group_members(child.pid), [], msg="no members")

    def test_start_time_is_the_wall_clock_time_the_process_started(self) -> None:
        """Together with the pid it identifies the process across pid reuse."""
        before = time.time()
        child = self.child(f"print({READY!r}, flush=True); import time; time.sleep(60)")
        start = present(self.table.start_time(child.pid))
        self.assertGreaterEqual(start, before - 1.0, msg="not before the spawn")
        self.assertLessEqual(start, time.time() + 1.0, msg="not in the future")

    def test_start_time_of_a_missing_process_is_none(self) -> None:
        """A reaped pid has no start time."""
        child = spawn_python(f"print({READY!r}, flush=True)")
        stop(child)
        self.assertIsNone(self.table.start_time(child.pid), msg="gone")

    def test_footprint_counts_dirty_memory(self) -> None:
        """A process holding 200 MiB of touched memory reports at least 150 MiB."""
        child = self.child(
            f"""
            data = bytearray(200 * 1024 * 1024)
            for index in range(0, len(data), 4096):
                data[index] = 1
            print({READY!r}, flush=True)
            import time; time.sleep(60)
            """
        )
        footprint = present(self.table.footprint(child.pid))
        self.assertGreater(footprint, 150 * MIB, msg="dirty memory is counted")

    def test_footprint_is_not_resident_size(self) -> None:
        """Clean file-backed pages count towards resident size but not towards the footprint."""
        with tempfile.NamedTemporaryFile() as backing:
            backing.truncate(200 * MIB)
            child = self.child(
                f"""
                import mmap
                handle = open({backing.name!r}, "rb")
                mapping = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
                total = 0
                for index in range(0, len(mapping), 4096):
                    total += mapping[index]
                print({READY!r}, flush=True)
                import time; time.sleep(60)
                """
            )
            resident_kib = int(
                subprocess.run(  # noqa: S603 - fixed argv, no shell
                    ["/bin/ps", "-o", "rss=", "-p", str(child.pid)],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout
            )
            footprint = present(self.table.footprint(child.pid))
            self.assertGreater(resident_kib * 1024, 150 * MIB, msg="the mapped pages are resident")
            self.assertLess(footprint, 100 * MIB, msg="but they are not part of the footprint")

    def test_environment_shows_the_marker_of_a_non_platform_binary(self) -> None:
        """The marker set at exec is readable through KERN_PROCARGS2."""
        env = {**os.environ, "AGENTSD_RUN_ID": "marker-123"}
        child = self.child(f"print({READY!r}, flush=True); import time; time.sleep(60)", env=env)
        environment = present(self.table.environment(child.pid))
        self.assertEqual(environment.get("AGENTSD_RUN_ID"), "marker-123", msg="marker is visible")
        self.assertNotIn("-c", environment, msg="arguments are not mistaken for the environment")

    def test_environment_of_a_platform_binary_has_no_marker(self) -> None:
        """SIP-protected Apple binaries show no environment (digest section 1)."""
        sleeper = subprocess.Popen(
            ["/bin/sleep", "60"], env={**os.environ, "AGENTSD_RUN_ID": "hidden"}
        )
        self.addCleanup(sleeper.wait)
        self.addCleanup(sleeper.send_signal, signal.SIGKILL)
        environment = self.table.environment(sleeper.pid)
        self.assertNotIn("AGENTSD_RUN_ID", environment or {}, msg="no marker visible")

    def test_environment_of_a_missing_process_is_none(self) -> None:
        """A reaped pid has no readable environment."""
        child = spawn_python(f"print({READY!r}, flush=True)")
        stop(child)
        self.assertIsNone(self.table.environment(child.pid), msg="gone")


if __name__ == "__main__":
    unittest.main()
