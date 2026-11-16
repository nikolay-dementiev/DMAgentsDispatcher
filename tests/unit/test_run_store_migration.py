"""Tests of the run store migration from schema version 1 to version 2 on real files."""

import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path

from agents_dispatcher.adapters.system.sqlite_run_store import SqliteRunStore
from agents_dispatcher.application.errors import RunStoreError
from agents_dispatcher.domain.run import RunId, RunState, TerminalReason

DONE = RunId("3f2b8c1e-5d4a-4b7e-9c3a-1f2e3d4c5b6a")
LIVE = RunId("9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d")
BACKUP_NAME = "runs.sqlite3.v1.bak"
V1_ROWS = (
    (
        *(DONE.value, "/work/a", "aaaa", DONE.branch_name, "TERMINAL", "COMPLETED", 100.0, 160.0),
        *(4242, 99.5, "2.1.304", None, DONE.branch_name, 1, 0, 60.0),
    ),
    (
        *(LIVE.value, "/work/b", "bbbb", LIVE.branch_name, "RUNNING", None, 200.0, None),
        *(5151, 199.5, "2.1.304", None, None, None, None, None),
    ),
)

# The schema exactly as shipped with the first milestone (version 1).
V1_SCHEMA = (
    "CREATE TABLE schema_meta (version INTEGER NOT NULL)",
    """CREATE TABLE runs (
        run_id TEXT PRIMARY KEY,
        repo TEXT NOT NULL,
        base_commit TEXT NOT NULL,
        branch TEXT NOT NULL,
        state TEXT NOT NULL,
        terminal_reason TEXT,
        started_at REAL NOT NULL,
        finished_at REAL,
        pgid INTEGER,
        pg_start_time REAL,
        cli_version TEXT NOT NULL,
        limit_hit TEXT,
        result_branch TEXT,
        commits_on_branch INTEGER,
        worker_exit_status INTEGER,
        duration_s REAL
    )""",
    "INSERT INTO schema_meta (version) VALUES (1)",
)


class MigrationCase(unittest.TestCase):
    """A temporary state directory that holds a version 1 store."""

    def setUp(self) -> None:
        """Write the version 1 file."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name) / "state"
        self.directory.mkdir(mode=0o700)
        self.path = self.directory / "runs.sqlite3"
        connection = sqlite3.connect(self.path, isolation_level=None)
        for statement in V1_SCHEMA:
            connection.execute(statement)
        marks = ", ".join("?" * 16)
        for row in V1_ROWS:
            connection.execute(f"INSERT INTO runs VALUES ({marks})", row)  # noqa: S608 - marks are "?" only
        connection.close()

    def open_store(self) -> SqliteRunStore:
        """Open the store the way the composition root does."""
        return SqliteRunStore(str(self.path), epoch_s=lambda: 1000.0, busy_s=2.0)

    def query(self, path: Path, sql: str) -> list[tuple[object, ...]]:
        """Run a read-only query on a file."""
        connection = sqlite3.connect(path)
        try:
            return connection.execute(sql).fetchall()
        finally:
            connection.close()


class MigrationTests(MigrationCase):
    """Opening a version 1 store."""

    def test_both_rows_survive_with_their_values(self) -> None:
        """Nothing is lost or changed by the rebuild."""
        store = self.open_store()
        done, live = store.get(DONE), store.get(LIVE)
        self.assertEqual(done.state, RunState.TERMINAL, msg="finished run state")
        self.assertEqual(done.terminal_reason, TerminalReason.COMPLETED, msg="finished reason")
        self.assertEqual(done.base_commit, "aaaa", msg="finished base commit")
        self.assertEqual(done.pgid, 4242, msg="finished pgid")
        self.assertEqual(done.duration_s, 60.0, msg="finished duration")
        self.assertEqual(live.state, RunState.RUNNING, msg="live run state")
        self.assertEqual(live.pg_start_time, 199.5, msg="live start time")
        self.assertEqual(live.cli_version, "2.1.304", msg="live cli version")

    def test_the_schema_is_version_five_with_the_new_columns(self) -> None:
        """Version row, added columns and relaxed constraints."""
        self.open_store()
        self.assertEqual(
            self.query(self.path, "SELECT version FROM schema_meta"), [(5,)], msg="version"
        )
        columns = {row[1]: row[3] for row in self.query(self.path, "PRAGMA table_info(runs)")}
        for added in (
            "task",
            "hard_s",
            "memory_bytes",
            "supervisor_pid",
            "supervisor_start_time",
            "stop_requested_at",
            "stop_reason",
            "report_json",
            "revision",
        ):
            self.assertIn(added, columns, msg=f"column {added} added")
        self.assertEqual(columns["base_commit"], 0, msg="base commit may be unknown at accept")
        self.assertEqual(columns["cli_version"], 0, msg="cli version may be unknown at accept")
        self.assertEqual(columns["branch"], 1, msg="branch is known at accept")

    def test_migrated_rows_have_no_report_and_no_task(self) -> None:
        """Values that v1 never recorded stay empty rather than invented."""
        self.open_store()
        rows = self.query(self.path, "SELECT task, report_json, revision FROM runs ORDER BY run_id")
        self.assertEqual(rows, [("", None, 0), ("", None, 0)], msg="empty, not invented")

    def test_a_private_backup_of_the_old_file_is_written_once(self) -> None:
        """The v1 bytes are kept next to the store with owner-only permissions."""
        self.open_store()
        backup = self.directory / BACKUP_NAME
        self.assertTrue(backup.is_file(), msg="backup exists")
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600, msg="owner only")
        self.assertEqual(
            self.query(backup, "SELECT version FROM schema_meta"), [(1,)], msg="old version"
        )
        self.assertEqual(self.query(backup, "SELECT count(*) FROM runs"), [(2,)], msg="old rows")
        before = backup.read_bytes()
        self.open_store()
        self.assertEqual(backup.read_bytes(), before, msg="a second open does not touch it")

    def test_an_existing_backup_is_never_overwritten(self) -> None:
        """A copy left by an earlier attempt stays as it is."""
        backup = self.directory / BACKUP_NAME
        backup.write_bytes(b"kept")
        self.open_store()
        self.assertEqual(backup.read_bytes(), b"kept", msg="the earlier copy survives")

    def test_a_fresh_store_is_version_five_and_needs_no_backup(self) -> None:
        """No old file, no backup."""
        self.path.unlink()
        self.open_store()
        self.assertEqual(
            self.query(self.path, "SELECT version FROM schema_meta"), [(5,)], msg="version"
        )
        self.assertFalse((self.directory / BACKUP_NAME).exists(), msg="nothing to back up")

    def test_a_store_newer_than_the_code_is_refused_with_a_remedy(self) -> None:
        """Version 6 is not guessed at."""
        self.open_store()
        connection = sqlite3.connect(self.path)
        connection.execute("UPDATE schema_meta SET version = 6")
        connection.commit()
        connection.close()
        with self.assertRaises(RunStoreError) as caught:
            self.open_store()
        message = str(caught.exception)
        self.assertIn("newer", message, msg="says why")
        self.assertIn("upgrade", message, msg="says what to do")
        self.assertEqual(
            self.query(self.path, "SELECT version FROM schema_meta"), [(6,)], msg="file untouched"
        )


if __name__ == "__main__":
    unittest.main()
