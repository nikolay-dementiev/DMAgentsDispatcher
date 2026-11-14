"""Tests of the composition root: what is built from the settings, and orphan closing on wait."""

import contextlib
import io
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import agents_dispatcher
from agents_dispatcher.adapters.system.run_owner_lock import FileRunOwners
from agents_dispatcher.adapters.system.settings_loader import load_settings, shipped_defaults_text
from agents_dispatcher.adapters.system.sqlite_run_store import SqliteRunStore
from agents_dispatcher.application.errors import (
    InputTooLargeError,
    LaunchLostError,
    RunStoreError,
)
from agents_dispatcher.application.ports import AcceptedRun, ProcessIdentity
from agents_dispatcher.application.settings import Settings, SettingsError
from agents_dispatcher.composition.cli import build_services
from agents_dispatcher.composition.wiring import (
    WorkerEnvironment,
    denied_read_paths,
    launcher_config,
)
from agents_dispatcher.domain.run import Limits, RunState, TaskSpec, TerminalReason
from agents_dispatcher.interfaces.cli.services import Services
from tests.support.fake_executable import FakeExecutable, SourceRef
from tests.support.git_repo import make_repo
from tests.support.run_spies import RUN_ID

CLAUDE_VERSION = SourceRef(
    kind="captured_output",
    cli_name="claude",
    cli_version="2.1.304",
    excerpt="2.1.304 (Claude Code)",
)
GONE_SUPERVISOR = ProcessIdentity(pgid=999_999, start_time_s=1.0)


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

    def settings(self, executable: str | None = None, extra: str = "") -> Settings:
        """Load settings whose state and run area live under the temporary root."""
        chosen = executable or str(self.claude_path)
        user = (
            f'[store]\npath = "{self.root}/state"\n'
            f'[run_area]\npath = "{self.root}/runs"\n'
            f'[profiles._template]\nexecutable = "{chosen}"\n{extra}'
        )
        return load_settings(
            defaults_text=shipped_defaults_text(), user_text=user, account_home=str(self.home)
        )

    def compose(
        self, settings: Settings, path: str | None = None, interpreter: str = "/usr/bin/python3"
    ) -> Services:
        """Wire the services the way the console script does."""
        return build_services(
            settings,
            account_home=str(self.home),
            account_user="example",
            interpreter=interpreter,
            path=path or str(self.bin),
            user_settings=str(self.home / "settings.toml"),
        )

    def store(self, settings: Settings) -> SqliteRunStore:
        """Open the run store the services use."""
        return SqliteRunStore(
            f"{settings.store_path}/runs.sqlite3", epoch_s=time.time, busy_s=settings.sqlite_busy_s
        )


class WiringTests(CompositionCase):
    """What the root builds."""

    def test_every_service_is_built_and_the_store_file_is_created_in_the_state_folder(self) -> None:
        """The run store lives at store.path/runs.sqlite3."""
        services = self.compose(self.settings())
        for name in ("start", "status", "stop", "wait", "report"):
            self.assertTrue(hasattr(getattr(services, name), name), msg=f"{name} can be called")
        self.assertTrue((self.root / "state" / "runs.sqlite3").is_file(), msg="store file")

    def test_an_explicit_executable_that_does_not_exist_is_refused_naming_the_key(self) -> None:
        """No silent fall back to another claude; no run is accepted for a missing worker."""
        settings = self.settings(executable=str(self.bin / "missing"))
        with self.assertRaises(SettingsError) as caught:
            self.compose(settings)
        self.assertIn("profiles._template.executable", str(caught.exception), msg="key named")

    def test_auto_finds_claude_on_the_given_path(self) -> None:
        """`auto` resolves through the search path, not through the caller's environment."""
        services = self.compose(self.settings(executable="auto"), path=str(self.bin))
        self.assertTrue(hasattr(services.start, "start"), msg="resolved through the path")

    def test_auto_without_claude_on_the_path_is_refused_with_a_remedy(self) -> None:
        """The message says what to set."""
        with self.assertRaises(SettingsError) as caught:
            self.compose(self.settings(executable="auto"), path=str(self.root / "empty"))
        message = str(caught.exception)
        self.assertIn("claude", message, msg="the tool is named")
        self.assertIn("profiles._template.executable", message, msg="the remedy is named")


class LauncherConfigTests(CompositionCase):
    """The supervisor is launched from the very package copy that runs `start`."""

    def test_the_bootstrap_and_the_package_root_come_from_the_running_package(self) -> None:
        """A copied or uninstalled tree supervises with its own code (amendment A5)."""
        settings = self.settings()
        environment = WorkerEnvironment(str(self.home), "example", "/usr/bin/python3", "/usr/bin")
        config = launcher_config(settings, environment, user_settings="/x/settings.toml")
        package_root = str(Path(agents_dispatcher.__file__).resolve().parents[1])
        self.assertEqual(config.leading_args, (package_root,), msg="the package root is passed")
        self.assertEqual(
            config.bootstrap,
            f"{package_root}/agents_dispatcher/composition/supervisor_bootstrap.py",
            msg="the bootstrap of the same copy",
        )
        self.assertTrue(Path(config.bootstrap).is_file(), msg="the bootstrap file exists")
        self.assertEqual(
            (config.interpreter, config.account_home, config.account_user, config.path),
            ("/usr/bin/python3", str(self.home), "example", "/usr/bin"),
            msg="the account and interpreter",
        )
        self.assertEqual(
            (config.store_path, config.user_settings),
            (settings.store_path, "/x/settings.toml"),
            msg="the store and the settings file",
        )
        self.assertGreater(config.write_timeout_s, 0, msg="the brief is written with a bound")


class StartWiringTests(CompositionCase):
    """The `start` use case receives the settings it consumes (not defaults of its own)."""

    def test_the_input_bound_setting_reaches_start(self) -> None:
        """A task over `security.input_bytes` is refused before the repository is looked at."""
        services = self.compose(self.settings(extra="[security]\ninput_bytes = 8\n"))
        with self.assertRaises(InputTooLargeError):
            services.start.start(TaskSpec(repo="/nowhere", task="x" * 9))

    def test_the_startup_setting_bounds_the_wait_for_the_supervisor(self) -> None:
        """A supervisor that never claims ends the run LAUNCH_LOST after `watchdog.startup_s`."""
        repo = make_repo(self.root / "repo")
        extra = '[watchdog]\nstartup_s = "0.4"\nscan_s = "0.05"\n'
        services = self.compose(self.settings(extra=extra), interpreter="/usr/bin/true")
        began = time.monotonic()
        with self.assertRaises(LaunchLostError):
            services.start.start(TaskSpec(repo=repo, task="fix"))
        elapsed = time.monotonic() - began
        self.assertLess(elapsed, 3.0, msg="bounded by startup_s, not by a default")

    def test_the_scan_setting_sets_how_often_start_looks_for_a_claim(self) -> None:
        """A claim recorded 0.3 s in is seen within a scan, long before the 5 s bound."""
        repo = make_repo(self.root / "repo")
        settings = self.settings(extra='[watchdog]\nstartup_s = "5"\nscan_s = "0.05"\n')
        store = self.store(settings)

        def claim_when_accepted() -> None:
            deadline = time.monotonic() + 4.0
            while time.monotonic() < deadline:
                runs = store.unfinished()
                if runs:
                    time.sleep(0.3)
                    store.claim(runs[0].run_id, GONE_SUPERVISOR)
                    return
                time.sleep(0.01)

        thread = threading.Thread(target=claim_when_accepted)
        thread.start()
        self.addCleanup(thread.join)
        began = time.monotonic()
        services = self.compose(settings, interpreter="/usr/bin/true")
        services.start.start(TaskSpec(repo=repo, task="fix"))
        self.assertLess(time.monotonic() - began, 2.0, msg="seen at the scan, not at the bound")


class SweepWarningTests(CompositionCase):
    """A stale run that cannot be closed is a warning on stderr, not a block on `start`."""

    def test_a_failing_close_is_printed_and_the_new_start_goes_on(self) -> None:
        """The warning names the stale run and the error; the new run is still started."""
        repo = make_repo(self.root / "repo")
        settings = self.settings(extra='[watchdog]\nstartup_s = "0.3"\nscan_s = "0.05"\n')
        store = self.store(settings)
        store.accept(AcceptedRun(RUN_ID, "/work/example", "fix", Limits(60.0, 1024)))
        services = self.compose(settings, interpreter="/usr/bin/true")
        message = "disk is full\x1b[2J"
        errors = io.StringIO()
        with (
            mock.patch(
                "agents_dispatcher.adapters.system.orphan_sweep.OrphanSweeper._close",
                side_effect=RunStoreError(message),
            ),
            contextlib.redirect_stderr(errors),
            self.assertRaises(LaunchLostError, msg="the new run was started (and then lost)"),
        ):
            services.start.start(TaskSpec(repo=repo, task="fix"))
        self.assertIn(RUN_ID.value, errors.getvalue(), msg="the broken run is named")
        self.assertIn("disk is full", errors.getvalue(), msg="and the error")
        self.assertNotIn("\x1b", errors.getvalue(), msg="controls are escaped")


class OrphanClosingTests(CompositionCase):
    """A wait closes a run whose supervisor is gone, and leaves a live one alone."""

    def running_run(self, settings: Settings) -> SqliteRunStore:
        """Accept, claim and start a run in the store."""
        store = self.store(settings)
        store.accept(AcceptedRun(RUN_ID, "/work/example", "fix", Limits(60.0, 1024)))
        store.claim(RUN_ID, GONE_SUPERVISOR)
        store.set_state(RUN_ID, RunState.RUNNING)
        return store

    def test_a_wait_closes_a_run_whose_supervisor_is_gone_as_orphaned(self) -> None:
        """The dead-owner close happens where somebody looks."""
        settings = self.settings()
        store = self.running_run(settings)
        result = self.compose(settings).wait.wait(RUN_ID, 0.05)
        self.assertTrue(result.ready, msg="the wait ended")
        stored = store.get(RUN_ID)
        self.assertEqual(stored.state, RunState.TERMINAL, msg="the run is closed")
        self.assertEqual(stored.terminal_reason, TerminalReason.ORPHANED, msg="as orphaned")

    def test_a_wait_closes_a_run_nobody_claimed_after_the_startup_bound_and_margin(self) -> None:
        """Accepted long ago, never claimed: LAUNCH_LOST, not a hang."""
        settings = self.settings()
        old = SqliteRunStore(
            f"{settings.store_path}/runs.sqlite3",
            epoch_s=lambda: time.time() - 3600,
            busy_s=settings.sqlite_busy_s,
        )
        old.accept(AcceptedRun(RUN_ID, "/work/example", "fix", Limits(60.0, 1024)))
        result = self.compose(settings).wait.wait(RUN_ID, 0.05)
        self.assertTrue(result.ready, msg="the run is closed")
        self.assertEqual(old.get(RUN_ID).terminal_reason, TerminalReason.LAUNCH_LOST, msg="reason")

    def test_a_wait_leaves_a_freshly_accepted_run_alone(self) -> None:
        """The supervisor still has its startup time."""
        settings = self.settings()
        store = self.store(settings)
        store.accept(AcceptedRun(RUN_ID, "/work/example", "fix", Limits(60.0, 1024)))
        result = self.compose(settings).wait.wait(RUN_ID, 0.05)
        self.assertFalse(result.ready, msg="still waiting for its supervisor")
        self.assertEqual(store.get(RUN_ID).state, RunState.ACCEPTED, msg="untouched")

    def test_a_live_supervisor_keeps_its_run_open(self) -> None:
        """The run's owner lock is held, so the close is refused."""
        settings = self.settings()
        store = self.running_run(settings)
        ownership = FileRunOwners(f"{settings.store_path}/control").hold(RUN_ID)
        self.addCleanup(ownership.release)
        result = self.compose(settings).wait.wait(RUN_ID, 0.05)
        self.assertFalse(result.ready, msg="the run is still going")
        self.assertEqual(store.get(RUN_ID).state, RunState.RUNNING, msg="untouched")
        self.assertTrue(result.status.owner_alive, msg="the owner is seen alive")


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

    def test_a_moved_supervisor_log_folder_is_denied_too(self) -> None:
        """The log holds proxy decisions; wherever the user puts it, the worker may not read it."""
        base = self.settings()
        moved = Settings(**{**base.__dict__, "logging_directory": f"{self.root}/elsewhere/logs"})
        self.assertIn(f"{self.root}/elsewhere/logs", denied_read_paths(moved), msg="log folder")

    def test_a_trimmed_selector_list_denies_less(self) -> None:
        """Only the selectors the settings name are expanded."""
        base = self.settings()
        trimmed = Settings(**{**base.__dict__, "read_deny": ("@sensitive-user-data",)})
        denied = denied_read_paths(trimmed)
        self.assertNotIn(base.store_path, denied, msg="control state not selected")
        self.assertIn(f"{self.home}/.ssh", denied, msg="inventory selected")


if __name__ == "__main__":
    unittest.main()
