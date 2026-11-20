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

from agents_dispatcher.adapters.system.decision_log import FileMcpLog
from agents_dispatcher.adapters.system.stdio_claim import ShutdownSignals

CLAIM_SCRIPT = """
import os, stat, subprocess, sys
from agents_dispatcher.adapters.system.stdio_claim import claim_stdio
claimed = claim_stdio()
print("stray print")
os.write(1, b"raw fd1\\n")
subprocess.run(["sh", "-c", "echo child output"], check=False)
os.write(claimed.out_fd, b"frame\\n")
os.write(claimed.out_fd, b"stdin:" + os.read(claimed.in_fd, 100) + b"\\n")
null = stat.S_ISCHR(os.fstat(0).st_mode) and os.read(0, 100) == b""
os.write(claimed.out_fd, b"fd0-null:" + str(null).encode() + b"\\n")
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
            b"frame\nstdin:hello\nfd0-null:True\n",
            msg="standard output holds exactly what went through the claimed descriptor",
        )
        for stray in (b"stray print", b"raw fd1", b"child output"):
            self.assertIn(stray, done.stderr, msg=f"{stray!r} went to standard error")


class FileMcpLogHardeningTests(unittest.TestCase):
    """The log cannot take the server down, whatever it is given."""

    def test_text_that_cannot_be_encoded_is_written_escaped(self) -> None:
        """A lone surrogate in a client name must not raise."""
        collected: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            FileMcpLog(directory, collected.append).record("INITIALIZED", "client=\ud800/1")
            text = (Path(directory) / "sdk.log").read_text(encoding="utf-8")
        self.assertIn("MCP INITIALIZED client=", text, msg="the line was written")

    def test_a_warning_that_raises_is_not_propagated(self) -> None:
        """A closed standard error must not turn a log failure into a crash."""
        with tempfile.TemporaryDirectory() as directory:
            blocked = Path(directory) / "file"
            blocked.write_text("x", encoding="utf-8")

            def broken(text: str) -> None:
                raise BrokenPipeError(text)

            FileMcpLog(str(blocked), broken).record("TOOL", "x")

    def test_failures_after_the_first_are_still_reported_when_the_log_recovers(self) -> None:
        """The warning names how many lines were lost once a write succeeds again."""
        warnings: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "logs"
            target.write_text("blocker", encoding="utf-8")
            log = FileMcpLog(str(target), warnings.append)
            log.record("TOOL", "a")
            log.record("TOOL", "b")
            target.unlink()
            log.record("TOOL", "c")
            text = (target / "sdk.log").read_text(encoding="utf-8")
        self.assertIn("MCP LOG_GAP lost=2", text, msg="the gap is written to the log itself")


if __name__ == "__main__":
    unittest.main()
