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
    "path_until",
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

    def test_a_run_that_is_already_quarantined_is_not_quarantined_again(self) -> None:
        """A second write would replace the outcome kept for the run."""
        store = self.make_store()
        self.quarantined(store)
        with self.assertRaises(RunStoreError, msg="refused"):
            store.quarantine(RUN_ID, WOULD_BE, ())
        self.assertEqual(store.survivors(RUN_ID), list(SURVIVORS), msg="the survivors are kept")

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


class ResolutionStoreTests(QuarantineStoreCase):
    """The ledger side of the quarantine commands: settle, release, verify and list."""

    SETTLED = replace(WOULD_BE, branch="agentsd-run-x", commits_on_branch=1, report_json="FINAL")

    def test_a_quarantined_run_is_shown_to_the_commands_with_what_they_need(self) -> None:
        """Repository, base commit, mode and the times of the release and the verification."""
        store = self.make_store()
        self.quarantined(store)
        held = present(store.held(RUN_ID))
        self.assertEqual(
            (held.state, held.repo, held.mode, held.released_at, held.absence_verified_at),
            (RunState.QUARANTINED, "/work/example", ROUTING.mode, None, None),
            "the held run",
        )
        self.assertIsNone(store.held(RunId(str(uuid.uuid4()))), "an unknown run is None")

    def test_settling_ends_the_run_with_the_outcome_the_time_and_a_free_slot(self) -> None:
        """One write: terminal, the report, the verification time; the kept outcome is gone."""
        store = self.make_store()
        self.quarantined(store)
        store.settle(RUN_ID, self.SETTLED, verified_at=7777.0)
        stored = store.get(RUN_ID)
        self.assertEqual(
            (stored.state, stored.terminal_reason, stored.result_branch, stored.report_json),
            (RunState.TERMINAL, TerminalReason.COMPLETED, "agentsd-run-x", "FINAL"),
            "the final record",
        )
        self.assertEqual(stored.absence_verified_at, 7777.0, "when absence was verified")
        self.assertIsNone(store.pending(RUN_ID), "the kept outcome is dropped")
        self.assertEqual(store.survivors(RUN_ID), [], "and so are the survivors")
        self.assertEqual(store.active("anthropic"), 0, "the provider slot is free")

    def test_only_a_quarantined_run_can_be_settled_or_released(self) -> None:
        """A running or finished run is not the quarantine's to end."""
        store = self.make_store()
        store.accept(accepted())
        with self.assertRaises(RunStoreError, msg="a run that is not quarantined"):
            store.settle(RUN_ID, self.SETTLED, verified_at=1.0)
        with self.assertRaises(RunStoreError, msg="a release of the same"):
            store.release(RUN_ID, self.SETTLED, note="n", released_at=1.0)

    def test_a_release_ends_the_run_frees_the_slot_and_keeps_the_survivors_to_check_later(
        self,
    ) -> None:
        """The risk is the owner's: absence is not verified, so the survivors stay recorded."""
        store = self.make_store()
        self.quarantined(store)
        failed = replace(WOULD_BE, terminal_reason=TerminalReason.FAILED)
        store.release(RUN_ID, failed, note="released by operator", released_at=8888.0)
        stored = store.get(RUN_ID)
        self.assertEqual(
            (stored.state, stored.terminal_reason, stored.released_at, stored.release_note),
            (RunState.TERMINAL, TerminalReason.FAILED, 8888.0, "released by operator"),
            "released",
        )
        self.assertIsNone(stored.absence_verified_at, "absence is not verified by a release")
        self.assertEqual(store.survivors(RUN_ID), list(SURVIVORS), "kept for the later look")
        self.assertEqual(store.active("anthropic"), 0, "the provider slot is free")

    def test_verifying_a_released_run_marks_it_and_drops_the_survivors(self) -> None:
        """After the processes are shown gone nothing is left to look for."""
        store = self.make_store()
        self.quarantined(store)
        store.release(RUN_ID, WOULD_BE, note="released by operator", released_at=8888.0)
        store.mark_verified(RUN_ID, verified_at=9999.0)
        self.assertEqual(store.get(RUN_ID).absence_verified_at, 9999.0, "the time")
        self.assertEqual(store.survivors(RUN_ID), [], "the survivors are dropped")
        with self.assertRaises(RunStoreError, msg="a second verification"):
            store.mark_verified(RUN_ID, verified_at=10000.0)

    def test_a_run_that_was_never_released_cannot_be_marked_verified(self) -> None:
        """Verification belongs to the released runs."""
        store = self.make_store()
        store.accept(accepted())
        store.finish(RUN_ID, WOULD_BE)
        with self.assertRaises(RunStoreError, msg="not released"):
            store.mark_verified(RUN_ID, verified_at=1.0)

    def test_the_runs_that_wait_for_a_look_are_the_quarantined_and_the_unverified_released(
        self,
    ) -> None:
        """Settled, verified and ordinary runs are not listed; the oldest quarantine comes first."""
        store = self.make_store()
        ids = [RunId(str(uuid.uuid4())) for _ in range(5)]
        for run in ids:
            store.accept(accepted(run))
            store.claim(run, SUPERVISOR)
            store.quarantine(run, WOULD_BE, SURVIVORS)
        store.settle(ids[0], self.SETTLED, verified_at=1.0)
        store.release(ids[1], WOULD_BE, note="released by operator", released_at=2.0)
        store.release(ids[2], WOULD_BE, note="released by operator", released_at=2.0)
        store.mark_verified(ids[2], verified_at=3.0)
        plain = RunId(str(uuid.uuid4()))
        store.accept(accepted(plain))
        self.assertEqual(store.awaiting(), [ids[1], ids[3], ids[4]], "waiting for a look")


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
        migrated.quarantine(fresh, WOULD_BE, SURVIVORS, path_until=1500.5)
        self.assertEqual(migrated.survivors(fresh), list(SURVIVORS), "the new table works")
        self.assertEqual(migrated.get(fresh).path_until, 1500.5, "and so does the folder bound")
        with closing(sqlite3.connect(self.path)) as reader:
            version = reader.execute("SELECT version FROM schema_meta").fetchone()
        self.assertEqual(version, (SCHEMA_VERSION,), "the version is current")

    def test_the_folder_bound_of_a_swept_run_is_kept_and_a_plain_quarantine_has_none(self) -> None:
        """Only a quarantine that names a bound stores one."""
        store = self.make_store()
        bounded, plain = RunId(str(uuid.uuid4())), RunId(str(uuid.uuid4()))
        for run in (bounded, plain):
            store.accept(accepted(run))
            store.claim(run, SUPERVISOR)
        store.quarantine(bounded, WOULD_BE, SURVIVORS, path_until=1500.5)
        store.quarantine(plain, WOULD_BE, SURVIVORS)
        self.assertEqual(store.get(bounded).path_until, 1500.5, "the bound is stored")
        self.assertIsNone(store.get(plain).path_until, "no bound unless one was given")

    def test_a_version_6_store_made_before_the_folder_bound_gets_the_column(self) -> None:
        """The same version, written by an earlier build, is completed and works."""
        store = self.make_store()
        run = RunId(str(uuid.uuid4()))
        store.accept(accepted(run))
        store.claim(run, SUPERVISOR)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("ALTER TABLE runs DROP COLUMN path_until")
            connection.commit()
        reopened = self.make_store()
        self.assertIsNone(reopened.get(run).path_until, "the old run reads back with no bound")
        reopened.quarantine(run, WOULD_BE, SURVIVORS, path_until=1500.5)
        self.assertEqual(reopened.get(run).path_until, 1500.5, "and a bound can be stored")

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
