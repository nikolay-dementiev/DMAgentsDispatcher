"""Tests of how the supervisor leaves a fault behind when it gives up."""

import io
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from agents_dispatcher.adapters.system.sqlite_run_store import SqliteRunStore
from agents_dispatcher.application.ports import AcceptedRun
from agents_dispatcher.composition.supervisor import EXIT_FAILED, run_supervisor
from agents_dispatcher.domain.run import Limits, RunId


class RecordFaultTests(unittest.TestCase):
    """A broken settings file is the simplest way to make the supervisor give up."""

    def setUp(self) -> None:
        """Create an account with unreadable settings and one accepted run."""
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        self.settings_file = self.root / "settings.toml"
        self.settings_file.write_text("this is [not toml", encoding="utf-8")
        self.run_id = RunId(str(uuid.uuid4()))
        self.store = SqliteRunStore(
            f"{self.root}/state/runs.sqlite3", epoch_s=time.time, busy_s=2.0
        )
        self.store.accept(AcceptedRun(self.run_id, "/work/example", "task", Limits(60.0, 1024)))

    def supervise(self) -> tuple[int, str]:
        """Run the supervisor in this process; return its exit code and what it wrote to stderr."""
        argv = [
            "--run-id",
            self.run_id.value,
            "--store-path",
            f"{self.root}/state",
            "--account-home",
            str(self.root),
            "--account-user",
            "example",
            "--user-settings",
            str(self.settings_file),
        ]
        stderr = io.StringIO()
        with mock.patch("sys.stderr", stderr):
            code = run_supervisor(argv, io.StringIO("task"))
        return code, stderr.getvalue()

    def test_an_unexpected_error_while_recording_keeps_the_original_exit_code(self) -> None:
        """The original failure stands; the second error is reported, not raised."""
        with mock.patch.object(
            SqliteRunStore, "record_fault", side_effect=[RuntimeError("disk exploded"), None]
        ) as record:
            code, stderr = self.supervise()
        self.assertEqual(code, EXIT_FAILED, msg="the exit code of the original failure")
        self.assertIn("disk exploded", stderr, msg="the second error is visible")
        self.assertEqual(record.call_count, 2, msg="a second attempt carries both causes")
        second = record.call_args_list[1].args[1]
        self.assertIn("settings", second, msg="the original cause is in the fault text")
        self.assertIn("disk exploded", second, msg="the second error is attached to it")

    def test_when_nothing_can_be_recorded_the_original_exit_code_still_stands(self) -> None:
        """Both attempts fail: still no exception, the exit code is the original one."""
        with mock.patch.object(
            SqliteRunStore, "record_fault", side_effect=RuntimeError("still broken")
        ):
            code, stderr = self.supervise()
        self.assertEqual(code, EXIT_FAILED, msg="the exit code of the original failure")
        self.assertIn("still broken", stderr, msg="the error is visible")


if __name__ == "__main__":
    unittest.main()
