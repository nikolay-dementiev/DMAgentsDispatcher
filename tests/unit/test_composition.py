"""Tests of the composition root wiring: what is built from the settings, and the start-up sweep."""

import fcntl
import os
import tempfile
import time
import unittest
from pathlib import Path

from agents_dispatcher.adapters.system.settings_loader import load_settings, shipped_defaults_text
from agents_dispatcher.adapters.system.sqlite_run_store import SqliteRunStore
from agents_dispatcher.application.ports import RunRecord
from agents_dispatcher.application.settings import Settings, SettingsError
from agents_dispatcher.composition.cli import (
    compose_run_service,
    denied_read_paths,
)
from agents_dispatcher.domain.run import RunState, TerminalReason
from tests.support.fake_executable import FakeExecutable, SourceRef
from tests.support.run_spies import BASE, RUN_ID

CLAUDE_VERSION = SourceRef(
    kind="captured_output",
    cli_name="claude",
    cli_version="2.1.304",
    excerpt="2.1.304 (Claude Code)",
)


class CompositionCase(unittest.TestCase):
    """A temporary account home with a fake claude executable."""

    def setUp(self) -> None:
        """Create the home, the fake executable and a settings loader."""
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name).resolve()
        self.home = self.root / "home"
        self.bin = self.root / "bin"
        self.home.mkdir()
        self.bin.mkdir()
        self.claude = FakeExecutable("claude", 'echo "2.1.304 (Claude Code)"\n', CLAUDE_VERSION)
        self.claude_path = self.claude.write_to(self.bin)

    def settings(self, executable: str | None = None) -> Settings:
        """Load settings whose state and run area live under the temporary root."""
        chosen = executable or str(self.claude_path)
        user = (
            f'[store]\npath = "{self.root}/state"\n'
            f'[run_area]\npath = "{self.root}/runs"\n'
            f'[profiles._template]\nexecutable = "{chosen}"\n'
        )
        return load_settings(
            defaults_text=shipped_defaults_text(), user_text=user, account_home=str(self.home)
        )

    def compose(self, settings: Settings, path: str | None = None) -> object:
        """Wire the service the way the console script does."""
        return compose_run_service(
            settings,
            account_home=str(self.home),
            account_user="example",
            interpreter="/usr/bin/python3",
            path=path or str(self.bin),
        )


class WiringTests(CompositionCase):
    """What the root builds."""

    def test_a_service_is_built_and_the_store_file_is_created_in_the_state_folder(self) -> None:
        """The run store lives at store.path/runs.sqlite3."""
        service = self.compose(self.settings())
        self.assertTrue(hasattr(service, "run"), msg="the service can run a task")
        self.assertTrue((self.root / "state" / "runs.sqlite3").is_file(), msg="store file")

    def test_an_explicit_executable_that_does_not_exist_is_refused_naming_the_key(self) -> None:
        """No silent fall back to another claude."""
        settings = self.settings(executable=str(self.bin / "missing"))
        with self.assertRaises(SettingsError) as caught:
            self.compose(settings)
        self.assertIn("profiles._template.executable", str(caught.exception), msg="key named")

    def test_auto_finds_claude_on_the_given_path(self) -> None:
        """`auto` resolves through the search path, not through the caller's environment."""
        service = self.compose(self.settings(executable="auto"), path=str(self.bin))
        self.assertTrue(hasattr(service, "run"), msg="resolved through the path")

    def test_auto_without_claude_on_the_path_is_refused_with_a_remedy(self) -> None:
        """The message says what to set."""
        with self.assertRaises(SettingsError) as caught:
            self.compose(self.settings(executable="auto"), path=str(self.root / "empty"))
        message = str(caught.exception)
        self.assertIn("claude", message, msg="the tool is named")
        self.assertIn("profiles._template.executable", message, msg="the remedy is named")


class StartupSweepTests(CompositionCase):
    """A run that an earlier supervisor left unfinished is closed when the next one starts."""

    def test_an_unfinished_run_is_closed_as_orphaned_at_start(self) -> None:
        """The sweep runs inside the root, before any new run."""
        settings = self.settings()
        store = SqliteRunStore(
            f"{settings.store_path}/runs.sqlite3", epoch_s=time.time, busy_s=settings.sqlite_busy_s
        )
        store.begin(RunRecord(RUN_ID, "/work/example", BASE, RUN_ID.branch_name, "2.1.304"))
        store.set_state(RUN_ID, RunState.RUNNING)
        self.compose(settings)
        stored = store.get(RUN_ID)
        self.assertEqual(stored.state, RunState.TERMINAL, msg="the run is closed")
        self.assertEqual(stored.terminal_reason, TerminalReason.ORPHANED, msg="as orphaned")


class ConcurrentSupervisorTests(CompositionCase):
    """A second `agentsd run` must not cancel the healthy run of a live supervisor."""

    def test_a_live_supervisor_keeps_its_unfinished_run_from_being_swept(self) -> None:
        """Another supervisor holds the shared lock, so the start-up sweep does not run."""
        settings = self.settings()
        store = SqliteRunStore(
            f"{settings.store_path}/runs.sqlite3", epoch_s=time.time, busy_s=settings.sqlite_busy_s
        )
        store.begin(RunRecord(RUN_ID, "/work/example", BASE, RUN_ID.branch_name, "2.1.304"))
        store.set_state(RUN_ID, RunState.RUNNING)
        descriptor = os.open(
            f"{settings.store_path}/supervisor.lock", os.O_CREAT | os.O_RDWR, 0o600
        )
        self.addCleanup(os.close, descriptor)
        fcntl.flock(descriptor, fcntl.LOCK_SH)
        self.compose(settings)
        self.assertEqual(store.get(RUN_ID).state, RunState.RUNNING, msg="the live run is untouched")

    def test_two_services_in_one_process_do_not_sweep_each_other(self) -> None:
        """The first service stays alive for the process, so a later start-up leaves its run be."""
        settings = self.settings()
        self.compose(settings)
        store = SqliteRunStore(
            f"{settings.store_path}/runs.sqlite3", epoch_s=time.time, busy_s=settings.sqlite_busy_s
        )
        store.begin(RunRecord(RUN_ID, "/work/example", BASE, RUN_ID.branch_name, "2.1.304"))
        store.set_state(RUN_ID, RunState.RUNNING)
        self.compose(settings)
        self.assertEqual(store.get(RUN_ID).state, RunState.RUNNING, msg="not swept by the second")


class DeniedReadTests(CompositionCase):
    """The read_deny selectors expand to concrete paths."""

    def test_every_selector_expands(self) -> None:
        """Inventories, the control state and the run area are all denied."""
        settings = self.settings()
        denied = denied_read_paths(settings)
        for expected in (
            f"{self.home}/.ssh",
            f"{self.home}/.codex",
            settings.store_path,
            settings.run_area_path,
        ):
            self.assertIn(expected, denied, msg=f"{expected} is denied")

    def test_a_trimmed_selector_list_denies_less(self) -> None:
        """Only the selectors the settings name are expanded."""
        base = self.settings()
        trimmed = Settings(**{**base.__dict__, "read_deny": ("@sensitive-user-data",)})
        denied = denied_read_paths(trimmed)
        self.assertNotIn(base.store_path, denied, msg="control state not selected")
        self.assertIn(f"{self.home}/.ssh", denied, msg="inventory selected")


if __name__ == "__main__":
    unittest.main()
