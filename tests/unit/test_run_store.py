"""Tests of the SQLite run store on a real database file in a temporary directory."""

import fcntl
import os
import sqlite3
import stat
import tempfile
import unittest
import uuid
from collections.abc import Callable
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest import mock

from agents_dispatcher.adapters.system.sqlite_run_store import SqliteRunStore
from agents_dispatcher.application.errors import CapacityContendedError, RunStoreError
from agents_dispatcher.application.ports import AcceptedRun, FinishRecord, ProcessIdentity
from agents_dispatcher.domain.routing import RunRouting, TaskMode
from agents_dispatcher.domain.run import LimitKind, Limits, RunId, RunState, TerminalReason
from tests.support.routing import ROUTING
from tests.support.run_spies import BASE, RUN_ID


def begin(store: SqliteRunStore, run_id: RunId = RUN_ID) -> None:
    """Store an accepted run whose supervisor has already recorded its workspace facts."""
    store.accept(AcceptedRun(run_id, "/work/example", "fix the bug", Limits(60.0, 1024), ROUTING))
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


ROUTING_COLUMNS = ("task_type", "mode", "provider", "model", "effort", "route_json")


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
        connection.execute("UPDATE schema_meta SET version = 5")
        connection.commit()
        connection.close()
        with self.assertRaises(RunStoreError):
            self.make_store()


def accepted(run_id: RunId, provider: str = "anthropic") -> AcceptedRun:
    """Return an accepted run routed to `provider`."""
    return AcceptedRun(
        run_id, "/work/example", "x", Limits(60.0, 1024), replace(ROUTING, provider=provider)
    )


SECOND = RunId("7a1c9e52-3b64-4d08-8f21-0c5d9a4e6b13")
THIRD = RunId("0b6f3e14-92a7-4c55-a1d8-5e7c2f9b4a60")


class CapacityTests(RunStoreCase):
    """The accepting transaction counts the provider's non-terminal runs (PLAN-9 P7)."""

    def test_a_provider_at_capacity_refuses_the_next_accept_and_writes_no_row(self) -> None:
        """One slot, one live run: the second is refused with the provider named."""
        store = self.make_store()
        store.accept(accepted(RUN_ID), capacity=1)
        with self.assertRaises(CapacityContendedError, msg="full") as caught:
            store.accept(accepted(SECOND), capacity=1)
        self.assertEqual(caught.exception.provider, "anthropic", msg="names the provider")
        self.assertIsNone(store.find(SECOND), msg="no row was written")

    def test_a_provider_below_capacity_accepts(self) -> None:
        """Two slots, one live run: the second goes in."""
        store = self.make_store()
        store.accept(accepted(RUN_ID), capacity=2)
        store.accept(accepted(SECOND), capacity=2)
        self.assertEqual(store.active("anthropic"), 2, msg="both counted")

    def test_a_terminal_run_frees_its_slot(self) -> None:
        """Only non-terminal runs count."""
        store = self.make_store()
        store.accept(accepted(RUN_ID), capacity=1)
        store.finish(RUN_ID, FINISH)
        store.accept(accepted(SECOND), capacity=1)
        self.assertEqual(store.active("anthropic"), 1, msg="only the new run counts")

    def test_providers_are_counted_apart(self) -> None:
        """A full provider does not block another one."""
        store = self.make_store()
        store.accept(accepted(RUN_ID, "anthropic"), capacity=1)
        store.accept(accepted(SECOND, "openai"), capacity=1)
        self.assertEqual(store.active("openai"), 1, msg="openai has its own slot")

    def test_no_capacity_means_no_limit(self) -> None:
        """The cap is applied only when given."""
        store = self.make_store()
        for run_id in (RUN_ID, SECOND, THIRD):
            store.accept(accepted(run_id))
        self.assertEqual(store.active("anthropic"), 3, msg="all accepted")

    def test_the_count_and_the_insert_share_one_write_transaction(self) -> None:
        """The count is read after BEGIN IMMEDIATE, so two writers cannot both see a free slot."""
        store = SqliteRunStore(self.path, epoch_s=lambda: 1.0, busy_s=2.0, connect=self.traced)
        store.accept(accepted(RUN_ID), capacity=2)
        self.trace.clear()
        store.accept(accepted(SECOND), capacity=2)
        statements = [
            sql
            for sql in self.trace
            if sql.startswith(("BEGIN", "SELECT COUNT", "INSERT", "COMMIT"))
        ]
        self.assertEqual(
            [" ".join(sql.split()[:2]) for sql in statements],
            ["BEGIN IMMEDIATE", "SELECT COUNT(*)", "INSERT INTO", "COMMIT"],
            msg="begin, count, insert, commit in one transaction",
        )


class StoreFaultTests(RunStoreCase):
    """The supervisor's fault text lives in the run row."""

    def test_a_recorded_fault_is_read_back_from_the_stored_run_and_the_snapshot(self) -> None:
        """Both the full row and the read-side view carry it."""
        store = self.make_store()
        store.accept(
            AcceptedRun(RUN_ID, "/work/example", "fix the bug", Limits(60.0, 1024), ROUTING)
        )
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
        store.accept(
            AcceptedRun(RUN_ID, "/work/example", "fix the bug", Limits(60.0, 1024), ROUTING)
        )
        self.assertIsNone(store.get(RUN_ID).fault, msg="no fault recorded")

    def test_a_fault_cannot_be_recorded_for_a_finished_or_unknown_run(self) -> None:
        """The same rule as every other update."""
        store = self.make_store()
        with self.assertRaises(RunStoreError, msg="unknown run"):
            store.record_fault(RUN_ID, "x")
        store.accept(
            AcceptedRun(RUN_ID, "/work/example", "fix the bug", Limits(60.0, 1024), ROUTING)
        )
        store.finish(RUN_ID, FINISH)
        with self.assertRaises(RunStoreError, msg="finished run"):
            store.record_fault(RUN_ID, "x")

    def test_a_version_two_store_gains_the_column_and_keeps_its_runs(self) -> None:
        """The owner's store from the previous release opens and keeps its rows."""
        store = self.make_store()
        store.accept(
            AcceptedRun(RUN_ID, "/work/example", "fix the bug", Limits(60.0, 1024), ROUTING)
        )
        connection = sqlite3.connect(self.path)
        connection.execute("ALTER TABLE runs DROP COLUMN fault")
        for column in ROUTING_COLUMNS:
            connection.execute(f"ALTER TABLE runs DROP COLUMN {column}")
        connection.execute("UPDATE schema_meta SET version = 2")
        connection.commit()
        connection.close()
        migrated = self.make_store()
        self.assertEqual(migrated.get(RUN_ID).task, "fix the bug", msg="the run survived")
        migrated.record_fault(RUN_ID, "now possible")
        self.assertEqual(migrated.get(RUN_ID).fault, "now possible", msg="the column works")


class StoreRoutingTests(RunStoreCase):
    """A run keeps its routing; a store written before routing existed still opens (schema 4)."""

    def test_the_routing_of_an_accepted_run_is_read_back(self) -> None:
        """Task type, mode, provider, model, effort and the route, in both read paths."""
        store = self.make_store()
        begin(store)
        self.assertEqual(store.get(RUN_ID).routing, ROUTING, msg="the stored run")
        snapshot = store.find(RUN_ID)
        assert snapshot is not None  # noqa: S101 - narrows the type for the checker
        self.assertEqual(snapshot.routing, ROUTING, msg="the snapshot of the read side")

    def test_a_running_read_run_has_no_branch_and_a_running_write_run_has_its_planned_one(
        self,
    ) -> None:
        """Found in the real read run: `status` named a branch that a read run never gets."""
        read_store = self.make_store()
        read_store.accept(
            AcceptedRun(
                RUN_ID,
                "/work/example",
                "look",
                Limits(60.0, 1024),
                replace(ROUTING, task_type="audit", mode=TaskMode.READ),
            )
        )
        read = read_store.find(RUN_ID)
        assert read is not None  # noqa: S101 - narrows the type for the checker
        self.assertIsNone(read.branch, msg="a read run has no result branch while it runs")
        write_store = self.make_store()
        other = RunId("7a1c9e52-3b64-4d08-8f21-0c5d9a4e6b13")
        write_store.accept(AcceptedRun(other, "/work/example", "x", Limits(60.0, 1024), ROUTING))
        write = write_store.find(other)
        assert write is not None  # noqa: S101 - narrows the type for the checker
        self.assertEqual(write.branch, other.branch_name, msg="the planned branch of a write run")

    def test_a_version_three_store_keeps_its_runs_and_reads_them_as_unrouted_writes(self) -> None:
        """No task type, mode write, provider anthropic, model and effort unknown, empty route."""
        store = self.make_store()
        begin(store)
        connection = sqlite3.connect(self.path)
        for column in ROUTING_COLUMNS:
            connection.execute(f"ALTER TABLE runs DROP COLUMN {column}")
        connection.execute("UPDATE schema_meta SET version = 3")
        connection.commit()
        connection.close()
        migrated = self.make_store()
        self.assertEqual(
            migrated.get(RUN_ID).routing,
            RunRouting(None, TaskMode.WRITE, "anthropic", None, None, ()),
            msg="the old row reads as an unrouted write run of anthropic",
        )
        self.assertEqual(migrated.get(RUN_ID).task, "fix the bug", msg="the run survived")
        with closing(sqlite3.connect(self.path)) as connection:
            version = connection.execute("SELECT version FROM schema_meta").fetchone()
        self.assertEqual(version[0], 4, msg="the store is now schema 4")

    def test_a_migrated_store_accepts_new_routed_runs(self) -> None:
        """The added columns work after the migration."""
        self.make_store()
        connection = sqlite3.connect(self.path)
        for column in ROUTING_COLUMNS:
            connection.execute(f"ALTER TABLE runs DROP COLUMN {column}")
        connection.execute("UPDATE schema_meta SET version = 3")
        connection.commit()
        connection.close()
        store = self.make_store()
        begin(store)
        self.assertEqual(store.get(RUN_ID).routing, ROUTING, msg="routed after the migration")


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


class StoreLegacyLockTests(RunStoreCase):
    """The global supervisor lock of M1 is gone; its leftover file is removed when unheld."""

    def lock_path(self) -> Path:
        """Return where M1 kept the lock: next to the store file."""
        return self.directory / "supervisor.lock"

    def test_an_unheld_m1_lock_file_is_removed_when_the_store_opens(self) -> None:
        """Nobody holds it, nothing reads it any more."""
        self.directory.mkdir(parents=True)
        self.lock_path().write_text("", encoding="utf-8")
        self.make_store()
        self.assertFalse(self.lock_path().exists(), msg="the leftover file is removed")

    def test_a_held_m1_lock_file_is_left_alone(self) -> None:
        """An M1 supervisor that is still alive keeps its lock."""
        self.directory.mkdir(parents=True)
        self.lock_path().write_text("", encoding="utf-8")
        descriptor = os.open(self.lock_path(), os.O_RDWR)
        self.addCleanup(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.make_store()
        self.assertTrue(self.lock_path().exists(), msg="a held lock is not removed")

    def open_warned(self) -> list[str]:
        """Open a store and return the warnings it gave."""
        warnings: list[str] = []
        SqliteRunStore(self.path, epoch_s=lambda: 0.0, busy_s=2.0, warn=warnings.append)
        return warnings

    def test_a_lock_name_that_is_not_a_regular_file_is_left_alone(self) -> None:
        """A FIFO or a device with that name is not M1's leftover and is never removed."""
        self.directory.mkdir(parents=True)
        os.mkfifo(self.lock_path())
        self.open_warned()
        self.assertTrue(self.lock_path().exists(), msg="the FIFO is still there")

    def test_a_lock_file_replaced_after_it_was_locked_is_not_removed(self) -> None:
        """The path is checked against the locked file just before the removal."""
        self.directory.mkdir(parents=True)
        self.lock_path().write_text("", encoding="utf-8")
        real_flock = fcntl.flock

        def swap_then_lock(descriptor: int, operation: int) -> None:
            real_flock(descriptor, operation)
            self.lock_path().rename(self.directory / "moved-away")
            self.lock_path().write_text("a different file", encoding="utf-8")

        with mock.patch("fcntl.flock", side_effect=swap_then_lock):
            self.open_warned()
        self.assertTrue(self.lock_path().exists(), msg="the replacement is not removed")

    def test_a_symlink_named_like_the_old_lock_is_left_alone_and_named(self) -> None:
        """The target is never opened for locking, written or removed."""
        self.directory.mkdir(parents=True)
        target = self.directory.parent / "elsewhere.txt"
        target.write_text("keep", encoding="utf-8")
        self.lock_path().symlink_to(target)
        warnings = self.open_warned()
        self.assertTrue(self.lock_path().is_symlink(), msg="the symlink is still there")
        self.assertEqual(target.read_text(encoding="utf-8"), "keep", msg="the target is untouched")
        self.assertEqual(len(warnings), 1, msg="one warning")
        self.assertIn(str(self.lock_path()), warnings[0], msg="the symlink is named")

    def test_a_lock_file_that_cannot_be_removed_is_a_warning_and_the_store_opens(self) -> None:
        """A cosmetic cleanup must never make the store unusable."""
        self.directory.mkdir(parents=True)
        self.lock_path().write_text("", encoding="utf-8")
        with mock.patch("pathlib.Path.unlink", side_effect=PermissionError("denied")):
            warnings = self.open_warned()
        self.assertEqual(len(warnings), 1, msg="one warning")
        self.assertIn("denied", warnings[0], msg="the cause is shown")
        self.assertTrue((self.directory / "runs.sqlite3").is_file(), msg="the store opened")

    def test_a_store_without_an_m1_lock_file_opens_normally(self) -> None:
        """The common case after the first open."""
        self.make_store()
        self.assertFalse(self.lock_path().exists(), msg="none is created")


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
