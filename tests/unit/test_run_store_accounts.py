"""Tests of the account facts in the run store: login losses and per-run observations (schema 5)."""

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from agents_dispatcher.adapters.system.sqlite_run_store import SCHEMA_VERSION, SqliteRunStore
from agents_dispatcher.application.errors import RunStoreError
from agents_dispatcher.application.ports import AcceptedRun, FinishRecord
from agents_dispatcher.domain.run import Limits, TerminalReason
from tests.support.routing import ROUTING
from tests.support.run_spies import RUN_ID

ACCEPTED = AcceptedRun(
    run_id=RUN_ID,
    repo="/work/example",
    task="fix the bug",
    limits=Limits(hard_s=60.0, memory_bytes=1024),
    routing=ROUTING,
)
LOST_LAUNCH = FinishRecord(TerminalReason.LAUNCH_LOST, None, 0, None, None, 0.0, "{}")


class AccountStoreCase(unittest.TestCase):
    """A temporary store."""

    def setUp(self) -> None:
        """Create the state directory path."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = str(Path(directory.name) / "state" / "runs.sqlite3")

    def make_store(self) -> SqliteRunStore:
        """Open a store with a clock that advances."""
        counter = iter(range(10_000))
        return SqliteRunStore(self.path, epoch_s=lambda: 1000.0 + next(counter), busy_s=2.0)


class LoginLossTests(AccountStoreCase):
    """A loss is recorded per provider and survives a restart."""

    def test_no_loss_is_recorded_in_a_new_store(self) -> None:
        """Nothing is lost until a run says so."""
        self.assertFalse(self.make_store().is_lost("openai"), msg="no loss")

    def test_a_marked_loss_is_durable_and_per_provider(self) -> None:
        """Another store instance sees it; another provider does not."""
        self.make_store().mark_lost("openai")
        reopened = self.make_store()
        self.assertTrue(reopened.is_lost("openai"), msg="the loss survives a restart")
        self.assertFalse(reopened.is_lost("anthropic"), msg="another provider is untouched")

    def test_a_cleared_loss_is_gone_and_clearing_twice_is_harmless(self) -> None:
        """Clear removes the mark."""
        store = self.make_store()
        store.mark_lost("openai")
        store.clear_lost("openai")
        store.clear_lost("openai")
        self.assertFalse(store.is_lost("openai"), msg="cleared")

    def test_marking_twice_keeps_one_loss(self) -> None:
        """A second mark does not fail or stack."""
        store = self.make_store()
        store.mark_lost("openai")
        store.mark_lost("openai")
        store.clear_lost("openai")
        self.assertFalse(store.is_lost("openai"), msg="one clear removes it")


class ObservationTests(AccountStoreCase):
    """Facts observed around a run are kept by phase."""

    def test_a_run_starts_with_no_observations(self) -> None:
        """Nothing before anything is recorded."""
        store = self.make_store()
        store.accept(ACCEPTED)
        self.assertEqual(store.observations(RUN_ID), {}, msg="empty")

    def test_phases_are_kept_side_by_side(self) -> None:
        """Recording one phase does not drop another."""
        store = self.make_store()
        store.accept(ACCEPTED)
        store.record_observation(RUN_ID, "pre_launch", {"login": "LOGGED_IN"})
        store.record_observation(RUN_ID, "post_run", {"login": "LOGGED_OUT"})
        self.assertEqual(
            store.observations(RUN_ID),
            {"pre_launch": {"login": "LOGGED_IN"}, "post_run": {"login": "LOGGED_OUT"}},
            msg="both phases",
        )

    def test_an_observation_raises_the_revision(self) -> None:
        """It is a durable change like any other."""
        store = self.make_store()
        store.accept(ACCEPTED)
        before = store.get(RUN_ID).revision
        store.record_observation(RUN_ID, "pre_launch", {"login": "LOGGED_IN"})
        self.assertEqual(store.get(RUN_ID).revision, before + 1, msg="revision +1")

    def test_an_unknown_or_finished_run_is_refused(self) -> None:
        """A run that is not live takes no observation."""
        store = self.make_store()
        with self.assertRaises(RunStoreError, msg="unknown run"):
            store.record_observation(RUN_ID, "pre_launch", {})
        store.accept(ACCEPTED)
        self.assertTrue(store.finish_unclaimed(RUN_ID, LOST_LAUNCH), msg="closed unclaimed")
        with self.assertRaises(RunStoreError, msg="finished run"):
            store.record_observation(RUN_ID, "post_run", {})


class MigrationTests(AccountStoreCase):
    """A store written at schema 4 gains the account facts and keeps its runs."""

    def test_a_version_4_store_is_migrated_in_place(self) -> None:
        """Runs survive; the new table and column work; the version is current."""
        store = self.make_store()
        store.accept(ACCEPTED)
        with closing(sqlite3.connect(self.path)) as connection:
            connection.execute("ALTER TABLE runs DROP COLUMN observations_json")
            connection.execute("DROP TABLE provider_marks")
            connection.execute("UPDATE schema_meta SET version = 4")
            connection.commit()
        migrated = self.make_store()
        self.assertEqual(migrated.get(RUN_ID).task, "fix the bug", msg="the run is kept")
        migrated.mark_lost("openai")
        migrated.record_observation(RUN_ID, "pre_launch", {"login": "LOGGED_IN"})
        self.assertTrue(migrated.is_lost("openai"), msg="marks work")
        self.assertEqual(
            migrated.observations(RUN_ID),
            {"pre_launch": {"login": "LOGGED_IN"}},
            msg="column works",
        )
        with closing(sqlite3.connect(self.path)) as reader:
            version = reader.execute("SELECT version FROM schema_meta").fetchone()
        self.assertEqual(version, (SCHEMA_VERSION,), msg="the version is current")

    def test_the_account_facts_arrived_with_schema_5(self) -> None:
        """Schema 5 added them; later schemas keep them."""
        self.assertGreaterEqual(SCHEMA_VERSION, 5, msg="schema 5 carries the account facts")


if __name__ == "__main__":
    unittest.main()
