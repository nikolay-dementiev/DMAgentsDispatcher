"""Tests of the process edge of the MCP server: its log, its signals and its stdio."""

import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from agents_dispatcher.adapters.system.stdio_claim import ShutdownSignals

from agents_dispatcher.adapters.system.decision_log import FileMcpLog

CLAIM_SCRIPT = """
import os, subprocess, sys
from agents_dispatcher.adapters.system.stdio_claim import claim_stdio
claimed = claim_stdio()
print("stray print")
os.write(1, b"raw fd1\\n")
subprocess.run(["sh", "-c", "echo child output"], check=False)
os.write(claimed.out_fd, b"frame\\n")
os.write(claimed.out_fd, b"stdin:" + os.read(claimed.in_fd, 100) + b"\\n")
os.write(claimed.out_fd, b"fd0:" + os.read(0, 100) + b"|\\n")
sys.stderr.flush()
"""


class FileMcpLogTests(unittest.TestCase):
    """One line per event, in the SDK log, without anything the caller sent."""

    def setUp(self) -> None:
        """Make a private folder."""
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.directory = Path(self._tmp.name) / "logs"
        self.warnings: list[str] = []

    def log(self) -> FileMcpLog:
        """Build the log over the folder."""
        return FileMcpLog(str(self.directory), self.warnings.append)

    def test_an_event_is_one_line_with_the_mcp_marker(self) -> None:
        """Time, MCP, event, detail."""
        self.log().record("SERVE_START", "transport=own version=1.2.3")
        text = (self.directory / "sdk.log").read_text(encoding="utf-8")
        self.assertRegex(
            text,
            r"^\S+ MCP SERVE_START transport=own version=1\.2\.3\n$",
            msg="line shape",
        )
        mode = (self.directory / "sdk.log").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600, msg="private file")

    def test_a_detail_with_line_breaks_stays_on_one_line(self) -> None:
        """A log line is never split by its content."""
        self.log().record("TOOL", "a\nb\r\nc")
        text = (self.directory / "sdk.log").read_text(encoding="utf-8")
        self.assertEqual(len(text.splitlines()), 1, msg="one line")
        self.assertIsNotNone(re.search(r"MCP TOOL a b c$", text.strip()), msg="collapsed")

    def test_an_unwritable_log_warns_once_and_never_raises(self) -> None:
        """The server must not die of its log, and the failure must not be silent."""
        self.directory.parent.mkdir(parents=True, exist_ok=True)
        self.directory.write_text("a file where the folder should be", encoding="utf-8")
        log = self.log()
        log.record("TOOL", "x")
        log.record("TOOL", "y")
        self.assertEqual(len(self.warnings), 1, msg="warned once")
        self.assertIn("sdk.log", self.warnings[0], msg="names the file")


class ShutdownSignalsTests(unittest.TestCase):
    """Signals set the shutdown event and nothing else."""

    def test_sigterm_sets_the_event_and_the_old_handler_returns(self) -> None:
        """Entering installs, leaving restores."""
        event = threading.Event()
        before = signal.getsignal(signal.SIGTERM)
        with ShutdownSignals(event):
            os.kill(os.getpid(), signal.SIGTERM)
            self.assertTrue(event.wait(2), msg="the event was set by SIGTERM")
        self.assertEqual(signal.getsignal(signal.SIGTERM), before, msg="handler restored")

    def test_sighup_and_sigint_also_end_the_server(self) -> None:
        """The terminal going away and Ctrl-C."""
        for number in (signal.SIGHUP, signal.SIGINT):
            event = threading.Event()
            with ShutdownSignals(event):
                os.kill(os.getpid(), number)
                self.assertTrue(event.wait(2), msg=f"{number.name} sets the event")


class ClaimStdioTests(unittest.TestCase):
    """Only the server's own frames may reach standard output."""

    def test_stray_writes_go_to_standard_error_and_frames_stay_clean(self) -> None:
        """A print, a raw write to fd 1 and a child's output must not reach the client."""
        done = subprocess.run(  # noqa: S603 - fixed interpreter and script
            [sys.executable, "-c", CLAIM_SCRIPT],
            input=b"hello",
            capture_output=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(done.returncode, 0, msg=done.stderr.decode())
        self.assertEqual(
            done.stdout,
            b"frame\nstdin:hello\nfd0:|\n",
            msg="standard output holds exactly what went through the claimed descriptor",
        )
        for stray in (b"stray print", b"raw fd1", b"child output"):
            self.assertIn(stray, done.stderr, msg=f"{stray!r} went to standard error")


if __name__ == "__main__":
    unittest.main()
