"""Tests of the SQLite run store on a real database file in a temporary directory."""

import sqlite3
import stat
import tempfile
import unittest
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from agents_dispatcher.adapters.system.sqlite_run_store import SqliteRunStore
from agents_dispatcher.application.errors import RunStoreError
from agents_dispatcher.application.ports import AcceptedRun, FinishRecord, ProcessIdentity
from agents_dispatcher.domain.run import LimitKind, Limits, RunId, RunState, TerminalReason
from tests.support.run_spies import BASE, RUN_ID


def begin(store: SqliteRunStore, run_id: RunId = RUN_ID) -> None:
    """Store an accepted run whose supervisor has already recorded its workspace facts."""
    store.accept(AcceptedRun(run_id, "/work/example", "fix the bug", Limits(60.0, 1024)))
    store.record_workspace(run_id, base_commit=BASE, cli_version="2.1.304")


FINISH = FinishRecord(
    terminal_reason=TerminalReason.RESOURCE_LIMIT,
    branch=RUN_ID.branch_name,
    commits_on_branch=2,
    limit_hit=LimitKind.MEMORY,
    worker_exit_status=None,
    duration_s=12.5,
    report_json='{"state": "TERMINAL"}',
)


class DropsSynchronous(sqlite3.Connection):
    """A connection that silently ignores the durability pragma, like a misconfigured build."""

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:  # noqa: ANN401 - mirrors sqlite3
        """Run a statement, except that `PRAGMA synchronous=...` does nothing."""
        if sql.upper().replace(" ", "").startswith("PRAGMASYNCHRONOUS="):
            return super().execute("SELECT 1")
        return super().execute(sql, *args)


class LockedForReads(sqlite3.Connection):
    """A connection whose SELECT statements fail the way a busy database does."""

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:  # noqa: ANN401 - mirrors sqlite3
        """Run a statement, except that every `SELECT ... FROM runs` is refused."""
        if sql.lstrip().upper().startswith("SELECT") and " FROM RUNS" in sql.upper():
            message = "database is locked"
            raise sqlite3.OperationalError(message)
        return super().execute(sql, *args)


class DropsBusyTimeout(sqlite3.Connection):
    """A connection that silently ignores the busy-timeout pragma."""

    def execute(self, sql: str, *args: Any) -> sqlite3.Cursor:  # noqa: ANN401 - mirrors sqlite3
        """Run a statement, except that `PRAGMA busy_timeout=...` does nothing."""
        if sql.upper().replace(" ", "").startswith("PRAGMABUSY_TIMEOUT="):
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
        return SqliteRunStore(self.path, epoch_s=lambda: ticks + next(counter), busy_s=2.0)


class StoreRoundTripTests(RunStoreCase):
    """What goes in comes out unchanged."""

    def test_an_accepted_run_is_stored_with_its_workspace_facts(self) -> None:
        """The first record carries the identity and the state ACCEPTED."""
        store = self.make_store()
        begin(store)
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
        begin(store)
        store.set_state(RUN_ID, RunState.RUNNING)
        store.set_process(RUN_ID, ProcessIdentity(pgid=4242, start_time_s=1.5))
        reopened = self.make_store().get(RUN_ID)
        self.assertEqual(reopened.state, RunState.RUNNING, msg="state persisted")
        self.assertEqual(reopened.pgid, 4242, msg="pgid persisted")
        self.assertEqual(reopened.pg_start_time, 1.5, msg="leader start time persisted")

    def test_an_unknown_leader_start_time_is_stored_as_unknown(self) -> None:
        """None stays None through the file; it is never turned into a number."""
        store = self.make_store()
        begin(store)
        store.set_process(RUN_ID, ProcessIdentity(pgid=4242, start_time_s=None))
        reopened = self.make_store().get(RUN_ID)
        self.assertEqual(reopened.pgid, 4242, msg="pgid persisted")
        self.assertIsNone(reopened.pg_start_time, msg="start time unknown")

    def test_finish_stores_the_terminal_outcome(self) -> None:
        """The stored state is TERMINAL and equals what the report will say."""
        store = self.make_store()
        begin(store)
        store.finish(RUN_ID, FINISH)
        stored = store.get(RUN_ID)
        self.assertEqual(stored.state, RunState.TERMINAL, msg="state")
        self.assertEqual(stored.terminal_reason, TerminalReason.RESOURCE_LIMIT, msg="reason")
        self.assertEqual(stored.limit_hit, LimitKind.MEMORY, msg="limit")
        self.assertEqual(stored.commits_on_branch, 2, msg="commits")
        self.assertEqual(stored.duration_s, 12.5, msg="duration")
        self.assertIsNotNone(stored.finished_at, msg="finish time recorded")


class StoreUnfinishedTests(RunStoreCase):
    """The start-up sweep reads the runs that never reached TERMINAL."""

    def test_unfinished_lists_only_runs_that_are_not_terminal(self) -> None:
        """A finished run is not listed; a running one is, with its group identity."""
        store = self.make_store()
        other = RunId("9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d")
        begin(store)
        store.set_state(RUN_ID, RunState.RUNNING)
        store.set_process(RUN_ID, ProcessIdentity(pgid=4242, start_time_s=1.5))
        begin(store, other)
        store.finish(other, FINISH)
        listed = store.unfinished()
        self.assertEqual([run.run_id for run in listed], [RUN_ID], msg="only the running run")
        self.assertEqual(listed[0].pgid, 4242, msg="group identity is available")


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
        begin(store)
        with self.assertRaises(RunStoreError):
            begin(store)

    def test_terminal_is_reached_only_through_finish(self) -> None:
        """set_state cannot skip the terminal record."""
        store = self.make_store()
        begin(store)
        with self.assertRaises(RunStoreError):
            store.set_state(RUN_ID, RunState.TERMINAL)

    def test_a_finished_run_cannot_change(self) -> None:
        """TERMINAL is final."""
        store = self.make_store()
        begin(store)
        store.finish(RUN_ID, FINISH)
        with self.assertRaises(RunStoreError, msg="state change after finish"):
            store.set_state(RUN_ID, RunState.RUNNING)
        with self.assertRaises(RunStoreError, msg="second finish"):
            store.finish(RUN_ID, FINISH)

    def test_a_newer_schema_version_is_refused(self) -> None:
        """A file written by a different schema is not guessed at."""
        self.make_store()
        connection = sqlite3.connect(self.path)
        connection.execute("UPDATE schema_meta SET version = 4")
        connection.commit()
        connection.close()
        with self.assertRaises(RunStoreError):
            self.make_store()


class StoreFaultTests(RunStoreCase):
    """The supervisor's fault text lives in the run row."""

    def test_a_recorded_fault_is_read_back_from_the_stored_run_and_the_snapshot(self) -> None:
        """Both the full row and the read-side view carry it."""
        store = self.make_store()
        store.accept(AcceptedRun(RUN_ID, "/work/example", "fix the bug", Limits(60.0, 1024)))
        store.record_fault(RUN_ID, "settings: cannot read /x: denied")
        self.assertEqual(
            store.get(RUN_ID).fault, "settings: cannot read /x: denied", msg="stored run"
        )
        snapshot = store.find(RUN_ID)
        self.assertIsNotNone(snapshot, msg="the run exists")
        self.assertEqual(
            getattr(snapshot, "fault", None), "settings: cannot read /x: denied", msg="snapshot"
        )

    def test_a_run_without_a_fault_has_none(self) -> None:
        """The default is no fault."""
        store = self.make_store()
        store.accept(AcceptedRun(RUN_ID, "/work/example", "fix the bug", Limits(60.0, 1024)))
        self.assertIsNone(store.get(RUN_ID).fault, msg="no fault recorded")

    def test_a_fault_cannot_be_recorded_for_a_finished_or_unknown_run(self) -> None:
        """The same rule as every other update."""
        store = self.make_store()
        with self.assertRaises(RunStoreError, msg="unknown run"):
            store.record_fault(RUN_ID, "x")
        store.accept(AcceptedRun(RUN_ID, "/work/example", "fix the bug", Limits(60.0, 1024)))
        store.finish(RUN_ID, FINISH)
        with self.assertRaises(RunStoreError, msg="finished run"):
            store.record_fault(RUN_ID, "x")

    def test_a_version_two_store_gains_the_column_and_keeps_its_runs(self) -> None:
        """The owner's store from the previous release opens and keeps its rows."""
        store = self.make_store()
        store.accept(AcceptedRun(RUN_ID, "/work/example", "fix the bug", Limits(60.0, 1024)))
        connection = sqlite3.connect(self.path)
        connection.execute("ALTER TABLE runs DROP COLUMN fault")
        connection.execute("UPDATE schema_meta SET version = 2")
        connection.commit()
        connection.close()
        migrated = self.make_store()
        self.assertEqual(migrated.get(RUN_ID).task, "fix the bug", msg="the run survived")
        migrated.record_fault(RUN_ID, "now possible")
        self.assertEqual(migrated.get(RUN_ID).fault, "now possible", msg="the column works")


class StoreFailureTests(RunStoreCase):
    """A database that cannot be opened or read is a store error that names the file."""

    def reads(self, store: SqliteRunStore) -> dict[str, Callable[[], object]]:
        """Return the four read operations of the store as callables by name."""
        run_id = RunId(str(uuid.uuid4()))
        return {
            "find": lambda: store.find(run_id),
            "get": lambda: store.get(run_id),
            "unfinished": store.unfinished,
            "stop_requested": lambda: store.stop_requested(run_id),
        }

    def test_a_read_that_the_database_refuses_is_a_store_error_naming_the_file(self) -> None:
        """No raw `sqlite3.OperationalError` reaches the caller."""
        store = SqliteRunStore(
            self.path,
            epoch_s=lambda: 0.0,
            busy_s=2.0,
            connect=lambda path: sqlite3.connect(
                path, isolation_level=None, factory=LockedForReads
            ),
        )
        for name, read in self.reads(store).items():
            with self.assertRaises(RunStoreError, msg=name) as caught:
                read()
            self.assertIn(self.path, str(caught.exception), msg=f"{name} names the store file")

    def test_a_connection_that_cannot_be_opened_is_a_store_error_naming_the_file(self) -> None:
        """The open itself fails: still a store error."""
        broken = {"on": False}

        def connect(path: str) -> sqlite3.Connection:
            if broken["on"]:
                message = "unable to open database file"
                raise sqlite3.OperationalError(message)
            return sqlite3.connect(path, isolation_level=None)

        store = SqliteRunStore(self.path, epoch_s=lambda: 0.0, busy_s=2.0, connect=connect)
        broken["on"] = True
        with self.assertRaises(RunStoreError) as caught:
            store.unfinished()
        self.assertIn(self.path, str(caught.exception), msg="the message names the store file")


class StoreDisciplineTests(RunStoreCase):
    """Durability settings, transaction mode and file permissions."""

    def test_a_connection_that_ignores_the_durability_pragma_is_refused(self) -> None:
        """The pragma is read back; a mismatch fails closed."""
        with self.assertRaises(RunStoreError) as caught:
            SqliteRunStore(
                self.path,
                epoch_s=lambda: 0.0,
                busy_s=2.0,
                connect=lambda path: sqlite3.connect(
                    path, isolation_level=None, factory=DropsSynchronous
                ),
            )
        self.assertIn("synchronous", str(caught.exception), msg="message names the pragma")

    def test_the_busy_timeout_comes_from_the_argument_and_is_read_back(self) -> None:
        """ER-2: the timeout is a setting, set on every connection and verified."""
        SqliteRunStore(self.path, epoch_s=lambda: 0.0, busy_s=3.0, connect=self.traced)
        self.assertIn("PRAGMA busy_timeout=3000", self.trace, msg="configured value is applied")
        with self.assertRaises(RunStoreError, msg="a connection that ignores the pragma") as caught:
            SqliteRunStore(
                self.path,
                epoch_s=lambda: 0.0,
                busy_s=3.0,
                connect=lambda path: sqlite3.connect(
                    path, isolation_level=None, factory=DropsBusyTimeout
                ),
            )
        self.assertIn("busy_timeout", str(caught.exception), msg="message names the pragma")

    def test_every_write_runs_in_an_immediate_transaction(self) -> None:
        """A deferred BEGIN could lose the race to a second writer."""
        store = SqliteRunStore(self.path, epoch_s=lambda: 0.0, busy_s=2.0, connect=self.traced)
        self.trace.clear()
        begin(store)
        store.set_state(RUN_ID, RunState.RUNNING)
        store.finish(RUN_ID, FINISH)
        begins = [s for s in self.trace if s.upper().startswith("BEGIN")]
        self.assertEqual(
            begins,
            ["BEGIN IMMEDIATE"] * 4,
            msg="one immediate begin per write: accept, facts, state, finish",
        )

    def test_the_state_directory_and_file_are_private(self) -> None:
        """Briefs and outputs may be sensitive: owner only."""
        self.make_store()
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700, msg="directory")
        self.assertEqual(stat.S_IMODE(Path(self.path).stat().st_mode), 0o600, msg="file")

    def test_the_journal_mode_is_delete(self) -> None:
        """No WAL side files: one file holds the whole store."""
        begin(self.make_store())
        names = sorted(p.name for p in self.directory.iterdir())
        self.assertEqual(names, ["runs.sqlite3"], msg="no -wal or -shm files")


if __name__ == "__main__":
    unittest.main()
