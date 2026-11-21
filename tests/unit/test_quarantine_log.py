"""Tests of the quarantine lines in the SDK log and of the time text they carry."""

import stat
import tempfile
import unittest
from pathlib import Path

from agents_dispatcher.adapters.system.decision_log import FileQuarantineLog, FileResidualLog
from agents_dispatcher.adapters.system.timestamps import iso_utc
from agents_dispatcher.application.errors import DecisionLogError
from tests.support.run_spies import RUN_ID


class QuarantineLogTests(unittest.TestCase):
    """One line per event, in the private file the other SDK lines use."""

    def setUp(self) -> None:
        """Create a private place for the log folder."""
        self._root = tempfile.TemporaryDirectory()
        self.addCleanup(self._root.cleanup)
        self.folder = Path(self._root.name) / "logs"
        self.log = FileQuarantineLog(str(self.folder))

    def test_a_line_is_appended_whole_and_kept_private(self) -> None:
        """The text arrives as one line and only the owner can read the file."""
        self.log.record("2026-11-21T10:00:00Z QUARANTINE_RELEASED run=abc")
        self.log.record("second\nline")
        path = self.folder / "sdk.log"
        self.assertEqual(
            path.read_text(encoding="utf-8").splitlines(),
            ["2026-11-21T10:00:00Z QUARANTINE_RELEASED run=abc", "second line"],
            msg="two events are two lines and a line break inside one is flattened",
        )
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600, msg="private file")

    def test_control_characters_in_a_line_are_made_visible_not_written(self) -> None:
        """An error text from outside must not move the cursor or recolour a terminal."""
        self.log.record("error=\x1b[31mred\x07 end")
        text = (self.folder / "sdk.log").read_text(encoding="utf-8")
        self.assertNotIn("\x1b", text, msg="no escape character reaches the file")
        self.assertNotIn("\x07", text, msg="and no bell")

    def test_a_log_that_cannot_be_written_is_an_error_not_silence(self) -> None:
        """A release that cannot be recorded is shown to the owner."""
        blocker = Path(self._root.name) / "file"
        blocker.write_text("x", encoding="utf-8")
        with self.assertRaises(DecisionLogError, msg="the failure surfaces"):
            FileQuarantineLog(str(blocker / "logs")).record("line")


class ResidualLogTests(unittest.TestCase):
    """One line per process that could not be ruled out, with the run and the time."""

    def test_each_name_is_one_line_with_the_run_and_a_utc_time(self) -> None:
        """A reader of the log can tie the line to the run and to the moment."""
        with tempfile.TemporaryDirectory() as root:
            log = FileResidualLog(root, epoch_s=lambda: 0.0, stamp=iso_utc)
            log.unreadable(RUN_ID, ("pid 5", "pid 6"))
            lines = (Path(root) / "sdk.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            lines,
            [
                f"1970-01-01T00:00:00Z UNREADABLE_RESIDUAL run={RUN_ID.value} pid 5",
                f"1970-01-01T00:00:00Z UNREADABLE_RESIDUAL run={RUN_ID.value} pid 6",
            ],
            msg="one line each",
        )

    def test_a_held_line_says_the_process_started_outside_the_runs_life(self) -> None:
        """The spared process may be older than the run or later than its last sign of life."""
        with tempfile.TemporaryDirectory() as root:
            log = FileResidualLog(root, epoch_s=lambda: 0.0, stamp=iso_utc)
            log.held(RUN_ID, ("pid 7",))
            lines = (Path(root) / "sdk.log").read_text(encoding="utf-8").splitlines()
        self.assertEqual(
            lines,
            [
                f"1970-01-01T00:00:00Z HELD_RESIDUAL run={RUN_ID.value} pid 7 "
                "started outside the run's life"
            ],
            msg="neither claims it came late nor that it came early",
        )

    def test_a_log_that_cannot_be_written_raises_the_log_error(self) -> None:
        """The caller decides what a lost line means; the log never hides it."""
        with tempfile.TemporaryDirectory() as root:
            blocker = Path(root) / "file"
            blocker.write_text("x", encoding="utf-8")
            log = FileResidualLog(str(blocker / "logs"), epoch_s=lambda: 0.0, stamp=iso_utc)
            with self.assertRaises(DecisionLogError, msg="surfaces"):
                log.unreadable(RUN_ID, ("pid 5",))


class TimestampTests(unittest.TestCase):
    """The text of an epoch time in a note or a log line."""

    def test_epoch_zero_is_the_start_of_1970_in_utc(self) -> None:
        """Whole seconds, always UTC, always with the Z suffix."""
        self.assertEqual(iso_utc(0.0), "1970-01-01T00:00:00Z", msg="epoch zero")

    def test_fractions_are_dropped_and_the_date_follows_the_clock(self) -> None:
        """A day later and 59.9 seconds in is still the same whole second."""
        self.assertEqual(iso_utc(86_400 + 59.9), "1970-01-02T00:00:59Z", msg="whole seconds")


if __name__ == "__main__":
    unittest.main()
