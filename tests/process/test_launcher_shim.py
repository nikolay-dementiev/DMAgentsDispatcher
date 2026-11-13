"""Tests of the launcher shim: a self-contained script run as `python -I -S -B <file> <fd>` (F1)."""

import contextlib
import json
import os
import resource
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from agents_dispatcher.adapters.system import launcher_shim
from tests.support.hard_timeout import HardTimeoutCase
from tests.support.narrow import present

TERM_GRACE_S = 0.6
WAIT_S = 8.0


class RunningShim:
    """A shim started the way the confinement starts it, with the control pipe on our side."""

    def __init__(self, directory: Path, argv: list[str], *, term_grace_s: float) -> None:
        """Spawn the shim in its own session and hand it the specification on standard input."""
        self.directory = directory
        self.stdout_file = directory / "worker.out"
        read_end, self._write_end = os.pipe()
        spec = {
            "argv": argv,
            "env": {"PATH": "/usr/bin:/bin", "SHIM_CONTROL_FD": str(read_end)},
            "cwd": str(directory),
            "stdin_path": os.devnull,
            "stdout_path": str(self.stdout_file),
            "stderr_path": str(directory / "worker.err"),
            "term_grace_s": term_grace_s,
        }
        self.process = subprocess.Popen(  # noqa: S603 - argv array of this interpreter, no shell
            [sys.executable, "-I", "-S", "-B", str(Path(launcher_shim.__file__)), str(read_end)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            pass_fds=(read_end,),
            start_new_session=True,
        )
        os.close(read_end)
        present(self.process.stdin).write(json.dumps(spec))
        present(self.process.stdin).close()

    def first_line(self) -> str:
        """Read the first line the shim reports; fail the test instead of hanging (F7)."""
        stream = present(self.process.stdout)
        readable, _, _ = select.select([stream], [], [], WAIT_S)
        if not readable:
            message = f"the shim reported nothing within {WAIT_S} s"
            raise AssertionError(message)
        return stream.readline().strip()

    def close_control(self) -> None:
        """Close our end of the control pipe: the shim sees end-of-file."""
        if self._write_end >= 0:
            os.close(self._write_end)
            self._write_end = -1

    def finish(self) -> int:
        """Close the control pipe and wait for the shim to exit."""
        self.close_control()
        return self.process.wait(timeout=WAIT_S)

    def cleanup(self) -> None:
        """Make sure nothing of the group is left."""
        self.close_control()
        with contextlib.suppress(ProcessLookupError):  # the group may already be gone
            os.killpg(self.process.pid, signal.SIGKILL)
        self.process.wait(timeout=WAIT_S)
        present(self.process.stdout).close()


def alive(pid: int) -> bool:
    """Return whether a process with this pid exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_for(path: Path) -> str:
    """Wait until a file has content and return it."""
    deadline = time.monotonic() + WAIT_S
    while time.monotonic() < deadline:
        if path.exists() and path.read_text(encoding="utf-8").strip():
            return path.read_text(encoding="utf-8")
        time.sleep(0.05)
    message = f"{path} did not get content"
    raise AssertionError(message)


class LauncherShimTests(HardTimeoutCase):
    """What the shim does with a worker, a vanished parent and a stubborn group."""

    def setUp(self) -> None:
        """Create a scratch directory."""
        super().setUp()
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.directory = Path(scratch.name)

    def start(self, argv: list[str], *, term_grace_s: float = TERM_GRACE_S) -> RunningShim:
        """Start a shim and register its cleanup."""
        shim = RunningShim(self.directory, argv, term_grace_s=term_grace_s)
        self.addCleanup(shim.cleanup)
        return shim

    def test_the_worker_cannot_write_core_dumps_even_when_the_parent_allows_them(self) -> None:
        """Layer 5: a crashing worker must not leave its memory on disk, and cannot undo that."""
        _soft, hard = resource.getrlimit(resource.RLIMIT_CORE)
        before = resource.getrlimit(resource.RLIMIT_CORE)
        self.addCleanup(resource.setrlimit, resource.RLIMIT_CORE, before)
        resource.setrlimit(resource.RLIMIT_CORE, (hard, hard))
        shim = self.start(["/bin/sh", "-c", "echo $(ulimit -c) $(ulimit -Hc)"])
        shim.first_line()
        self.assertEqual(
            shim.stdout_file.read_text(encoding="utf-8"), "0 0\n", msg="soft and hard core limits"
        )

    def test_it_runs_the_worker_and_reports_its_exit_status(self) -> None:
        """The first line carries the exit status; the output lands in the named file."""
        shim = self.start([sys.executable, "-c", "print('hello'); raise SystemExit(3)"])
        self.assertEqual(shim.first_line(), "EXIT 3", msg="exit status reported")
        self.assertEqual(shim.stdout_file.read_text(encoding="utf-8"), "hello\n", msg="stdout")
        self.assertEqual(shim.finish(), 0, msg="the shim itself exits cleanly after the close")

    def test_a_worker_killed_by_a_signal_is_reported_with_a_negative_status(self) -> None:
        """Signal deaths are distinguishable from exit codes."""
        shim = self.start([sys.executable, "-c", "import os; os.kill(os.getpid(), 9)"])
        self.assertEqual(shim.first_line(), "EXIT -9", msg="killed by signal 9")

    def test_the_worker_does_not_inherit_the_control_pipe(self) -> None:
        """If it did, closing our end would never signal end-of-file."""
        code = (
            "import os\n"
            "fd = int(os.environ['SHIM_CONTROL_FD'])\n"
            "try:\n"
            "    os.fstat(fd)\n"
            "    print('inherited')\n"
            "except OSError:\n"
            "    print('closed')\n"
        )
        shim = self.start([sys.executable, "-c", code])
        shim.first_line()
        self.assertEqual(shim.stdout_file.read_text(encoding="utf-8"), "closed\n", msg="fd closed")

    def test_end_of_file_stops_a_worker_that_ignores_sigterm_and_its_background_child(self) -> None:
        """TERM, a short grace period, then KILL of the whole group."""
        code = (
            "trap '' TERM\n"
            "sleep 120 &\n"
            f"echo $! > {self.directory}/grandchild.pid\n"
            f"echo $$ > {self.directory}/worker.pid\n"
            "while true; do sleep 1; done\n"
        )
        shim = self.start(["/bin/sh", "-c", code])
        worker = int(wait_for(self.directory / "worker.pid"))
        grandchild = int(wait_for(self.directory / "grandchild.pid"))
        self.assertTrue(alive(worker) and alive(grandchild), msg="both run before the close")
        started = time.monotonic()
        shim.close_control()
        shim.process.wait(timeout=WAIT_S)
        self.assertLess(time.monotonic() - started, TERM_GRACE_S + 5.0, msg="bounded stop")
        deadline = time.monotonic() + 5.0
        while (alive(worker) or alive(grandchild)) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(alive(worker), msg="the worker is gone")
        self.assertFalse(alive(grandchild), msg="the background child is gone")

    def test_a_cooperative_worker_gets_sigterm_before_sigkill(self) -> None:
        """A worker that handles TERM can clean up."""
        code = (
            "import signal, sys, time, pathlib\n"
            f"marker = pathlib.Path({str(self.directory / 'terminated')!r})\n"
            "def handler(*_):\n"
            "    marker.write_text('yes')\n"
            "    sys.exit(0)\n"
            "signal.signal(signal.SIGTERM, handler)\n"
            f"pathlib.Path({str(self.directory / 'ready')!r}).write_text('1')\n"
            "time.sleep(120)\n"
        )
        shim = self.start([sys.executable, "-c", code], term_grace_s=5.0)
        wait_for(self.directory / "ready")
        shim.close_control()
        shim.process.wait(timeout=WAIT_S)
        self.assertEqual(
            (self.directory / "terminated").read_text(encoding="utf-8"), "yes", msg="TERM first"
        )

    def test_a_default_worker_dies_of_sigterm_without_waiting_for_the_grace_period(self) -> None:
        """The shim's own TERM handling must not leak into the worker as an ignored signal."""
        shim = self.start(["/bin/sleep", "120"], term_grace_s=30.0)
        deadline = time.monotonic() + 5.0
        while not shim.stdout_file.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.3)
        started = time.monotonic()
        shim.close_control()
        shim.process.wait(timeout=WAIT_S)
        self.assertLess(time.monotonic() - started, 6.0, msg="TERM was enough; no 30 s wait")

    def test_it_is_the_leader_of_its_own_group_and_the_worker_is_in_it(self) -> None:
        """The group is the shim's pid, so the supervisor is never in it."""
        shim = self.start([sys.executable, "-c", "import os; print(os.getpgrp())"])
        shim.first_line()
        self.assertEqual(
            shim.stdout_file.read_text(encoding="utf-8").strip(),
            str(shim.process.pid),
            msg="the worker's group is the shim's pid",
        )
        self.assertNotEqual(os.getpgrp(), shim.process.pid, msg="the test process is outside")

    def test_a_missing_executable_is_reported_not_swallowed(self) -> None:
        """A launch failure is a distinct line, so the caller can map it to LAUNCH_LOST."""
        shim = self.start(["/nonexistent/worker"])
        self.assertTrue(shim.first_line().startswith("LAUNCH_FAILED "), msg="launch failure line")


if __name__ == "__main__":
    unittest.main()
