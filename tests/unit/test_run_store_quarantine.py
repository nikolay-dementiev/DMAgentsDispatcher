"""Tests of the quarantine in the run store: the state, the survivors and the schema 6 migration."""

import sqlite3
import tempfile
import unittest
import uuid
from contextlib import closing
from dataclasses import replace
from pathlib import Path

from agents_dispatcher.adapters.system.sqlite_run_store import SCHEMA_VERSION, SqliteRunStore
from agents_dispatcher.application.errors import CapacityContendedError, RunStoreError
from agents_dispatcher.application.ports import AcceptedRun, FinishRecord, ProcessIdentity
from agents_dispatcher.domain.run import (
    LimitKind,
    Limits,
    RunId,
    RunState,
    Survivor,
    TerminalReason,
)
from agents_dispatcher.domain.views import QuarantineInfo, StopDisposition
from tests.support.narrow import present
from tests.support.routing import ROUTING
from tests.support.run_spies import RUN_ID

SUPERVISOR = ProcessIdentity(pgid=4321, start_time_s=55.5)
WOULD_BE = FinishRecord(
    terminal_reason=TerminalReason.COMPLETED,
    branch=RUN_ID.branch_name,
    commits_on_branch=2,
    limit_hit=None,
    worker_exit_status=0,
    duration_s=3.5,
    report_json='{"state": "TERMINAL"}',
)
SURVIVORS = (
    Survivor(pid=901, start_time=1234.5, sources=("group", "marker")),
    Survivor(pid=902, start_time=1240.25, sources=("folder",)),
)
NEW_COLUMNS = (
    "quarantined_at",
    "released_at",
    "absence_verified_at",
    "release_note",
    "pending_json",
)


def accepted(run_id: RunId = RUN_ID) -> AcceptedRun:
    """Return a run accepted for the default provider."""
    return AcceptedRun(run_id, "/work/example", "fix the bug", Limits(60.0, 1024), ROUTING)


class QuarantineStoreCase(unittest.TestCase):
    """A temporary store with a clock that advances one second per reading."""

    def setUp(self) -> None:
        """Create the path of the store."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = str(Path(directory.name) / "state" / "runs.sqlite3")

    def make_store(self) -> SqliteRunStore:
        """Open the store."""
        counter = iter(range(10_000))
        return SqliteRunStore(self.path, epoch_s=lambda: 1000.0 + next(counter), busy_s=2.0)

    def quarantined(self, store: SqliteRunStore) -> None:
        """Put the default run through the states to QUARANTINED."""
        store.accept(accepted())
        store.claim(RUN_ID, SUPERVISOR)
        store.set_state(RUN_ID, RunState.FINALIZING)
        store.quarantine(RUN_ID, WOULD_BE, SURVIVORS)


class QuarantineTests(QuarantineStoreCase):
    """Entering the quarantine and what is kept."""

    def test_the_run_is_quarantined_with_its_time_its_survivors_and_its_own_outcome(self) -> None:
        """One write keeps all three, so that no reader sees half of them."""
        store = self.make_store()
        self.quarantined(store)
        stored = store.get(RUN_ID)
        self.assertEqual(stored.state, RunState.QUARANTINED, "the state")
        self.assertIsNone(stored.terminal_reason, "not terminal: no reason yet")
        self.assertIsNotNone(stored.quarantined_at, "the time is kept")
        self.assertEqual(store.survivors(RUN_ID), list(SURVIVORS), "the survivors, in order")
        self.assertEqual(store.pending(RUN_ID), WOULD_BE, "what the run would have ended as")

    def test_the_read_side_sees_the_survivors_and_the_time_of_a_quarantined_run(self) -> None:
        """The snapshot carries them, so a status never needs a second read."""
        store = self.make_store()
        self.quarantined(store)
        found = present(store.find(RUN_ID))
        stored = store.get(RUN_ID)
        self.assertEqual(
            found.quarantine,
            QuarantineInfo(since=present(stored.quarantined_at), survivors=SURVIVORS),
            "since and survivors",
        )

    def test_a_run_that_is_not_quarantined_has_no_quarantine_in_its_snapshot(self) -> None:
        """Only a held-back run has the record."""
        store = self.make_store()
        store.accept(accepted())
        self.assertIsNone(present(store.find(RUN_ID)).quarantine, "none")

    def test_the_outcome_with_a_limit_round_trips(self) -> None:
        """A limit that ended the worker is part of what is kept."""
        store = self.make_store()
        store.accept(accepted())
        store.claim(RUN_ID, SUPERVISOR)
        limited = replace(
            WOULD_BE, terminal_reason=TerminalReason.RESOURCE_LIMIT, limit_hit=LimitKind.OUTPUT
        )
        store.quarantine(RUN_ID, limited, SURVIVORS)
        self.assertEqual(store.pending(RUN_ID), limited, "the limit survives the store")

    def test_a_quarantined_run_still_holds_its_provider_slot(self) -> None:
        """Capacity one: a second run is refused while the first is quarantined."""
        store = self.make_store()
        self.quarantined(store)
        self.assertEqual(store.active("anthropic"), 1, "it counts as active")
        other = RunId(str(uuid.uuid4()))
        with self.assertRaises(CapacityContendedError, msg="the slot is held"):
            store.accept(accepted(other), capacity=1)

    def test_quarantine_raises_the_revision_and_is_listed_among_the_unfinished(self) -> None:
        """A watcher sees the change, and the sweep sees a run that is not over."""
        store = self.make_store()
        store.accept(accepted())
        store.claim(RUN_ID, SUPERVISOR)
        before = store.get(RUN_ID).revision
        store.quarantine(RUN_ID, WOULD_BE, SURVIVORS)
        self.assertEqual(store.get(RUN_ID).revision, before + 1, "revision +1")
        self.assertEqual([item.run_id for item in store.unfinished()], [RUN_ID], "unfinished")

    def test_a_finished_or_unknown_run_cannot_be_quarantined(self) -> None:
        """Only a run that is not over can enter it."""
        store = self.make_store()
        with self.assertRaises(RunStoreError, msg="unknown run"):
            store.quarantine(RUN_ID, WOULD_BE, SURVIVORS)
        store.accept(accepted())
        store.finish(RUN_ID, WOULD_BE)
        with self.assertRaises(RunStoreError, msg="finished run"):
            store.quarantine(RUN_ID, WOULD_BE, SURVIVORS)

    def test_set_state_does_not_quarantine(self) -> None:
        """The survivors and the outcome must come with the state."""
        store = self.make_store()
        store.accept(accepted())
        with self.assertRaises(RunStoreError, msg="QUARANTINED only through quarantine"):
            store.set_state(RUN_ID, RunState.QUARANTINED)

    def test_a_quarantined_run_can_still_be_finished(self) -> None:
        """The re-check that finds nothing left ends the run through the ordinary finish."""
        store = self.make_store()
        self.quarantined(store)
        store.finish(RUN_ID, WOULD_BE)
        self.assertEqual(store.get(RUN_ID).state, RunState.TERMINAL, "terminal after finish")

    def test_a_stop_request_for_a_quarantined_run_is_too_late(self) -> None:
        """Nothing runs under the SDK's control any more."""
        store = self.make_store()
        self.quarantined(store)
        self.assertIs(store.request_stop(RUN_ID, "enough"), StopDisposition.TOO_LATE, "too late")


class MigrationTests(QuarantineStoreCase):
    """A store written at schema 5 gains the quarantine and keeps every run."""

    def test_the_current_version_is_6(self) -> None:
        """A change of schema is a conscious edit of this test."""
        self.assertEqual(SCHEMA_VERSION, 6, "schema 6 carries the quarantine")

    def test_a_version_5_store_with_a_run_in_every_state_is_migrated_in_place(self) -> None:
        """Every state and every terminal reason survives; the new parts work afterwards."""
        store = self.make_store()
        expected: dict[str, tuple[RunState, TerminalReason | None]] = {}
        reasons: list[TerminalReason] = list(TerminalReason)
        steps = (
            RunState.ACCEPTED,
            RunState.STARTING,
            RunState.RUNNING,
            RunState.STOPPING,
            RunState.FINALIZING,
        )
        for state in steps:
            run = RunId(str(uuid.uuid4()))
            store.accept(accepted(run))
            if state is not RunState.ACCEPTED:
                store.claim(run, SUPERVISOR)
            if state not in (RunState.ACCEPTED, RunState.STARTING):
                store.set_state(run, state)
            expected[run.value] = (state, None)
        for reason in reasons:
            run = RunId(str(uuid.uuid4()))
            store.accept(accepted(run))
            store.finish(run, replace(WOULD_BE, terminal_reason=reason))
            expected[run.value] = (RunState.TERMINAL, reason)
        with closing(sqlite3.connect(self.path)) as connection:
            for column in NEW_COLUMNS:
                connection.execute(f"ALTER TABLE runs DROP COLUMN {column}")
            connection.execute("DROP TABLE quarantine_survivors")
            connection.execute("UPDATE schema_meta SET version = 5")
            connection.commit()
        migrated = self.make_store()
        for value, (state, kept) in expected.items():
            stored = migrated.get(RunId(value))
            self.assertEqual((stored.state, stored.terminal_reason), (state, kept), value)
        fresh = RunId(str(uuid.uuid4()))
        migrated.accept(accepted(fresh))
        migrated.claim(fresh, SUPERVISOR)
        migrated.quarantine(fresh, WOULD_BE, SURVIVORS)
        self.assertEqual(migrated.survivors(fresh), list(SURVIVORS), "the new table works")
        with closing(sqlite3.connect(self.path)) as reader:
            version = reader.execute("SELECT version FROM schema_meta").fetchone()
        self.assertEqual(version, (SCHEMA_VERSION,), "the version is current")

    def test_a_newer_version_is_still_refused(self) -> None:
        """The next version is not guessed at."""
        self.make_store()
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("UPDATE schema_meta SET version = 7")
            connection.commit()
        with self.assertRaises(RunStoreError, msg="newer schema"):
            self.make_store()


if __name__ == "__main__":
    unittest.main()
