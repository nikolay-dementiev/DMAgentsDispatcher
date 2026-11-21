"""Tests that the confinement attributes processes to a run by more than the environment marker.

A worker can start a child that leaves the process group, clears its environment and changes its
directory. The run must still find it (O1 layer 3), and must still leave alone a process that
merely sat in the run folder before the run began.
"""

import contextlib
import os
import signal
import subprocess
import sys
import time

from agents_dispatcher.domain.run import LimitKind, Limits
from tests.process.test_seatbelt_confinement import MIB, ConfinementCase, alive
from tests.support.narrow import present

WAIT_S = 5.0
POLL_S = 0.05
HIDING_CHILD = 'import os, time; os.chdir("/"); time.sleep(120)'  # nothing left that names the run


def wait_gone(pid: int) -> bool:
    """Wait until the pid no longer exists, at most `WAIT_S`; return whether it is gone."""
    deadline = time.monotonic() + WAIT_S
    while alive(pid) and time.monotonic() < deadline:
        time.sleep(POLL_S)
    return not alive(pid)


def end_quietly(pid: int) -> None:
    """Kill a process the test started, if a failed assertion left it alive."""
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGKILL)


class HiddenDescendantTests(ConfinementCase):
    """Children that remove every trace except their ancestry or their folder."""

    def test_a_child_that_left_the_group_cleared_its_environment_and_moved_is_still_killed(
        self,
    ) -> None:
        """Seen while its parent lived, remembered by pid and start time, killed at the end."""
        pid_file = self.sandbox / "hidden.pid"
        outcome = self.execute(
            f"""
            import pathlib, subprocess, sys, time
            child = subprocess.Popen([sys.executable, "-B", "-c", {HIDING_CHILD!r}],
                                     env={{}}, start_new_session=True,
                                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
            pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))
            time.sleep(1.5)
            """
        )
        hidden = int(pid_file.read_text(encoding="utf-8"))
        self.assertTrue(wait_gone(hidden), msg="the hidden child was killed")
        self.assertTrue(any(str(hidden) in entry for entry in outcome.survivors), msg="reported")

    def test_the_memory_of_a_hidden_descendant_counts(self) -> None:
        """Memory is summed over the tracked tree, not only over the group and the marker."""
        outcome = self.execute(
            """
            import subprocess, sys, time
            code = ("import os, time\\nos.chdir('/')\\nd = bytearray(400*1024*1024)\\n"
                    "for i in range(0, len(d), 4096): d[i] = 1\\ntime.sleep(120)")
            subprocess.Popen([sys.executable, "-B", "-c", code], env={},
                             start_new_session=True, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(120)
            """,
            limits=Limits(hard_s=60.0, memory_bytes=150 * MIB),
        )
        self.assertEqual(outcome.limit_hit, LimitKind.MEMORY, msg="hidden memory counted")
        self.assertEqual(outcome.unresolved, (), msg="and the hidden process was stopped")

    def test_a_daemon_that_double_forked_is_found_through_the_folder_it_stayed_in(self) -> None:
        """No ancestry, marker, group or open log: only its working directory names the run."""
        pid_file = self.sandbox / "daemon.pid"
        daemon = (
            "import os, pathlib, time; "
            f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(120)"
        )
        outcome = self.execute(
            f"""
            import os, pathlib, sys, time
            if os.fork() == 0:
                os.setsid()
                if os.fork() == 0:
                    null = os.open("/dev/null", os.O_RDWR)
                    for stream in (0, 1, 2):
                        os.dup2(null, stream)
                    os.execve(sys.executable, [sys.executable, "-B", "-c", {daemon!r}], {{}})
                os._exit(0)
            deadline = time.monotonic() + 3
            while not pathlib.Path({str(pid_file)!r}).exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            """
        )
        self.assertEqual(outcome.exit_status, 0, msg="the worker itself finished")
        daemon_pid = int(pid_file.read_text(encoding="utf-8"))
        self.assertTrue(wait_gone(daemon_pid), msg="the daemon was killed through its folder")
        self.assertTrue(
            any(str(daemon_pid) in entry for entry in outcome.survivors), msg="and reported"
        )

    def test_a_grandchild_started_after_its_parent_was_reparented_is_still_killed(self) -> None:
        """Descendants of a remembered process are followed, though its own parent is long gone."""
        pid_file = self.sandbox / "grandchild.pid"
        launcher = (
            "import subprocess, sys\n"
            "def hide(code):\n"
            "    subprocess.Popen([sys.executable, '-B', '-c', code], env={}, cwd='/',\n"
            "                     start_new_session=True, stdin=subprocess.DEVNULL,\n"
            "                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        )
        grandchild = (
            "import os, pathlib, time\n"
            f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid()))\n"
            "time.sleep(120)\n"
        )
        middle = f"{launcher}import time\ntime.sleep(1.2)\nhide({grandchild!r})\ntime.sleep(120)\n"
        outer = f"{launcher}import time\nhide({middle!r})\ntime.sleep(0.6)\n"
        (self.sandbox / "outer.py").write_text(outer, encoding="utf-8")
        outcome = self.execute(
            f"""
            import subprocess, sys, time
            subprocess.Popen([sys.executable, "-B", {str(self.sandbox / "outer.py")!r}],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            time.sleep(2.8)
            """
        )
        self.assertTrue(pid_file.exists(), msg="the grandchild started during the run")
        grandchild_pid = int(pid_file.read_text(encoding="utf-8"))
        self.addCleanup(end_quietly, grandchild_pid)
        self.assertTrue(wait_gone(grandchild_pid), msg="the grandchild was killed")
        self.assertTrue(
            any(str(grandchild_pid) in entry for entry in outcome.survivors),
            msg="and it was found through the reparented middle process",
        )


class ChurnTests(ConfinementCase):
    """Other processes being started on the machine must not blind the watch or the cleanup."""

    SPAWNERS = 12

    def test_a_machine_busy_starting_processes_neither_loses_observation_nor_holds_the_run(
        self,
    ) -> None:
        """Short-lived processes refuse their environment for an instant; the look must cope."""
        for _ in range(self.SPAWNERS):
            loop = subprocess.Popen(
                ["/bin/sh", "-c", "while :; do /usr/bin/true; done"],
                start_new_session=True,
            )
            self.addCleanup(loop.wait)
            self.addCleanup(loop.kill)
        outcome = self.execute("import time\ntime.sleep(4)\n")
        self.assertIsNone(outcome.observation_lost, msg="the run was observed all the way")
        self.assertEqual(outcome.unresolved, (), msg="and the cleanup showed nothing left")
        self.assertFalse(outcome.cleanup_uncertain, msg="so the run is not held")


class OlderProcessTests(ConfinementCase):
    """A process that was already there is not the run's."""

    def test_a_process_that_sat_in_the_run_folder_before_the_run_is_left_alone(self) -> None:
        """The guard: the owner's shell in the folder is reported at most, never killed."""
        bystander = subprocess.Popen(
            [
                sys.executable,
                "-B",
                "-c",
                "print('ready', flush=True); import time; time.sleep(120)",
            ],
            cwd=self.sandbox,
            env={**os.environ},
            start_new_session=True,
            stdout=subprocess.PIPE,
            text=True,
        )
        self.addCleanup(present(bystander.stdout).close)
        self.addCleanup(bystander.wait)
        self.addCleanup(bystander.kill)
        present(bystander.stdout).readline()
        outcome = self.execute("print('done')")
        self.assertIsNone(bystander.poll(), msg="the bystander was not touched")
        self.assertEqual(outcome.survivors, (), msg="and it is not reported as the run's")
