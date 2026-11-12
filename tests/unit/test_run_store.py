"""Tests of the SQLite run store on a real database file in a temporary directory."""

import os
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from typing import Any

from agents_dispatcher.adapters.system.sqlite_run_store import SqliteRunStore
from agents_dispatcher.application.errors import RunStoreError
from agents_dispatcher.application.ports import FinishRecord, ProcessIdentity, RunRecord
from agents_dispatcher.domain.run import LimitKind, RunState, TerminalReason
from tests.support.run_spies import BASE, RUN_ID

RECORD = RunRecord(
    run_id=RUN_ID,
    repo="/work/example",
    base_commit=BASE,
    branch=RUN_ID.branch_name,
    cli_version="2.1.304",
)
FINISH = FinishRecord(
    terminal_reason=TerminalReason.RESOURCE_LIMIT,
    branch=RUN_ID.branch_name,
    commits_on_branch=2,
    limit_hit=LimitKind.MEMORY,
    worker_exit_status=None,
    duration_s=12.5,
)


class DropsSynchronous(sqlite3.Connection):
    """A connection that silently ignores the durability pragma, like a misconfigured build."""

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:  # noqa: ANN401 - mirrors sqlite3
        """Run a statement, except that `PRAGMA synchronous=...` does nothing."""
        if sql.upper().replace(" ", "").startswith("PRAGMASYNCHRONOUS="):
            return super().execute("SELECT 1")
        return super().execute(sql, *args)


class RunStoreCase(unittest.TestCase):
    """A temporary state directory plus helpers."""

    def setUp(self) -> None:
        """Create the directory that holds the store file."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name) / "state"
        self.path = str(self.directory / "runs.sqlite3")
        self.trace: list[str] = []

    def traced(self, path: str) -> sqlite3.Connection:
        """Open a connection that records every statement it runs."""
        connection = sqlite3.connect(path, isolation_level=None)
        connection.set_trace_callback(self.trace.append)
        return connection

    def make_store(self, ticks: float = 1000.0) -> SqliteRunStore:
        """Open a store whose wall clock advances by one second per call."""
        counter = iter(range(10_000))
        return SqliteRunStore(self.path, epoch_s=lambda: ticks + next(counter))


class StoreRoundTripTests(RunStoreCase):
    """What goes in comes out unchanged."""

    def test_begin_stores_an_accepted_run(self) -> None:
        """The first record carries the identity and the state ACCEPTED."""
        store = self.make_store()
        store.begin(RECORD)
        stored = store.get(RUN_ID)
        self.assertEqual(stored.state, RunState.ACCEPTED, msg="state")
        self.assertEqual(stored.repo, "/work/example", msg="repo")
        self.assertEqual(stored.base_commit, BASE, msg="base commit")
        self.assertEqual(stored.cli_version, "2.1.304", msg="cli version")
        self.assertEqual(stored.started_at, 1000.0, msg="started at from the injected wall clock")
        self.assertIsNone(stored.terminal_reason, msg="no reason yet")

    def test_state_and_process_identity_are_stored(self) -> None:
        """The group identity survives a reopen of the file."""
        store = self.make_store()
        store.begin(RECORD)
        store.set_state(RUN_ID, RunState.RUNNING)
        store.set_process(RUN_ID, ProcessIdentity(pgid=4242, start_time_s=1.5))
        reopened = self.make_store().get(RUN_ID)
        self.assertEqual(reopened.state, RunState.RUNNING, msg="state persisted")
        self.assertEqual(reopened.pgid, 4242, msg="pgid persisted")
        self.assertEqual(reopened.pg_start_time, 1.5, msg="leader start time persisted")

    def test_finish_stores_the_terminal_outcome(self) -> None:
        """The stored state is TERMINAL and equals what the report will say."""
        store = self.make_store()
        store.begin(RECORD)
        store.finish(RUN_ID, FINISH)
        stored = store.get(RUN_ID)
        self.assertEqual(stored.state, RunState.TERMINAL, msg="state")
        self.assertEqual(stored.terminal_reason, TerminalReason.RESOURCE_LIMIT, msg="reason")
        self.assertEqual(stored.limit_hit, LimitKind.MEMORY, msg="limit")
        self.assertEqual(stored.commits_on_branch, 2, msg="commits")
        self.assertEqual(stored.duration_s, 12.5, msg="duration")
        self.assertIsNotNone(stored.finished_at, msg="finish time recorded")


class StoreRefusalTests(RunStoreCase):
    """Wrong calls are errors, never silent."""

    def test_an_unknown_run_is_refused(self) -> None:
        """No update may create or ignore a run."""
        store = self.make_store()
        with self.assertRaises(RunStoreError):
            store.set_state(RUN_ID, RunState.RUNNING)

    def test_a_duplicate_begin_is_refused(self) -> None:
        """A run id is used once."""
        store = self.make_store()
        store.begin(RECORD)
        with self.assertRaises(RunStoreError):
            store.begin(RECORD)

    def test_terminal_is_reached_only_through_finish(self) -> None:
        """set_state cannot skip the terminal record."""
        store = self.make_store()
        store.begin(RECORD)
        with self.assertRaises(RunStoreError):
            store.set_state(RUN_ID, RunState.TERMINAL)

    def test_a_finished_run_cannot_change(self) -> None:
        """TERMINAL is final."""
        store = self.make_store()
        store.begin(RECORD)
        store.finish(RUN_ID, FINISH)
        with self.assertRaises(RunStoreError, msg="state change after finish"):
            store.set_state(RUN_ID, RunState.RUNNING)
        with self.assertRaises(RunStoreError, msg="second finish"):
            store.finish(RUN_ID, FINISH)

    def test_a_newer_schema_version_is_refused(self) -> None:
        """A file written by a different schema is not guessed at."""
        self.make_store()
        connection = sqlite3.connect(self.path)
        connection.execute("UPDATE schema_meta SET version = 2")
        connection.commit()
        connection.close()
        with self.assertRaises(RunStoreError):
            self.make_store()


class StoreDisciplineTests(RunStoreCase):
    """Durability settings, transaction mode and file permissions."""

    def test_a_connection_that_ignores_the_durability_pragma_is_refused(self) -> None:
        """The pragma is read back; a mismatch fails closed."""
        with self.assertRaises(RunStoreError) as caught:
            SqliteRunStore(
                self.path,
                epoch_s=lambda: 0.0,
                connect=lambda path: sqlite3.connect(
                    path, isolation_level=None, factory=DropsSynchronous
                ),
            )
        self.assertIn("synchronous", str(caught.exception), msg="message names the pragma")

    def test_every_write_runs_in_an_immediate_transaction(self) -> None:
        """A deferred BEGIN could lose the race to a second writer."""
        store = SqliteRunStore(self.path, epoch_s=lambda: 0.0, connect=self.traced)
        self.trace.clear()
        store.begin(RECORD)
        store.set_state(RUN_ID, RunState.RUNNING)
        store.finish(RUN_ID, FINISH)
        begins = [s for s in self.trace if s.upper().startswith("BEGIN")]
        self.assertEqual(begins, ["BEGIN IMMEDIATE"] * 3, msg="one immediate begin per write")

    def test_the_state_directory_and_file_are_private(self) -> None:
        """Briefs and outputs may be sensitive: owner only."""
        self.make_store()
        self.assertEqual(stat.S_IMODE(os.stat(self.directory).st_mode), 0o700, msg="directory")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600, msg="file")

    def test_the_journal_mode_is_delete(self) -> None:
        """No WAL side files: one file holds the whole store."""
        self.make_store().begin(RECORD)
        names = sorted(p.name for p in self.directory.iterdir())
        self.assertEqual(names, ["runs.sqlite3"], msg="no -wal or -shm files")


if __name__ == "__main__":
    unittest.main()
