"""Composition root of the agentsd command: the only place that builds concrete objects."""

import os
import shutil
import sys
import time
from pathlib import Path

from agents_dispatcher.adapters.system.claude_worker import DEFAULT_PATH, ClaudeCodeWorker
from agents_dispatcher.adapters.system.egress_proxy import ConnectAllowlistProxy, ProxyConfig
from agents_dispatcher.adapters.system.git_workspace import GitWorkspace
from agents_dispatcher.adapters.system.orphan_sweep import OrphanSweeper
from agents_dispatcher.adapters.system.package_version import PackageMetadataVersionSource
from agents_dispatcher.adapters.system.process_table import LibprocProcessTable
from agents_dispatcher.adapters.system.seatbelt_confinement import (
    ConfinementConfig,
    SeatbeltConfinement,
)
from agents_dispatcher.adapters.system.settings_loader import (
    account_home_dir,
    account_user_name,
    load_settings,
    shipped_defaults_text,
    user_settings_path,
)
from agents_dispatcher.adapters.system.sqlite_run_store import SqliteRunStore
from agents_dispatcher.adapters.system.supervisor_lock import SupervisorLock
from agents_dispatcher.adapters.system.system_clock import SuspendInclusiveClock
from agents_dispatcher.adapters.system.uuid_ids import UuidRunIds
from agents_dispatcher.application.run_task import RunTask
from agents_dispatcher.application.settings import Settings, SettingsError
from agents_dispatcher.domain.run import Limits
from agents_dispatcher.interfaces.cli.app import main as run_cli

STORE_FILE = "runs.sqlite3"
LOCK_FILE = "supervisor.lock"
EXECUTABLE_KEY = "profiles._template.executable"
WORKER_NAME = "claude"
# Kept for the life of the process: the shared lock must stay held while a run may be active.
_LIVE_LOCKS: list[SupervisorLock] = []


def main() -> int:
    """Entry point of the console script."""
    return run_cli(
        sys.argv[1:], sys.stdout, sys.stderr, PackageMetadataVersionSource(), build_run_service
    )


def build_run_service() -> RunTask:
    """Load the settings of the real account and wire the service; called only for `run`."""
    account_home = account_home_dir()
    return compose_run_service(
        _load_account_settings(account_home),
        account_home=account_home,
        account_user=account_user_name(),
        interpreter=sys.executable,
        path=DEFAULT_PATH,
    )


def compose_run_service(
    settings: Settings, *, account_home: str, account_user: str, interpreter: str, path: str
) -> RunTask:
    """Wire every adapter, close the runs an earlier supervisor left behind, return the use case."""
    executable = _resolve_executable(settings.worker_executable, path)
    clock = SuspendInclusiveClock()
    table = LibprocProcessTable()
    store = SqliteRunStore(
        f"{settings.store_path}/{STORE_FILE}", epoch_s=time.time, busy_s=settings.sqlite_busy_s
    )
    lock = SupervisorLock(f"{settings.store_path}/{LOCK_FILE}")
    if lock.try_exclusive():  # no other supervisor is alive, so every unfinished run is orphaned
        OrphanSweeper(
            store, table, term_grace_s=settings.term_grace_s, kill_wait_s=settings.kill_wait_s
        ).sweep()
    lock.stay_alive()
    _LIVE_LOCKS.append(lock)
    return RunTask(
        workspace=GitWorkspace(settings.run_area_path),
        egress=ConnectAllowlistProxy(_proxy_config(settings)),
        worker=ClaudeCodeWorker(
            executable=executable, home=account_home, user=account_user, path=path
        ),
        confinement=SeatbeltConfinement(
            ConfinementConfig(
                denied_reads=denied_read_paths(settings),
                term_grace_s=settings.term_grace_s,
                kill_wait_s=settings.kill_wait_s,
                scan_s=settings.watchdog_scan_s,
                interpreter=interpreter,
            ),
            table=table,
            clock=clock,
        ),
        store=store,
        clock=clock,
        ids=UuidRunIds(),
        default_limits=Limits(hard_s=settings.hard_s, memory_bytes=settings.memory_bytes),
    )


def denied_read_paths(settings: Settings) -> tuple[str, ...]:
    """Expand the `sandbox.read_deny` selectors into the paths the profile denies reading."""
    expansion = {
        "@sensitive-user-data": settings.sensitive_user_data,
        "@other-vendor-state": settings.other_vendor_state,
        "@sdk-control": (settings.store_path,),
        "@other-runs": (settings.run_area_path,),
    }
    return tuple(path for selector in settings.read_deny for path in expansion[selector])


def _proxy_config(settings: Settings) -> ProxyConfig:
    """Map the proxy settings onto the proxy's own configuration."""
    return ProxyConfig(
        hosts=settings.anthropic_hosts,
        ports=settings.proxy_ports,
        allow_nonpublic=settings.proxy_allow_nonpublic,
        header_bytes=settings.proxy_header_bytes,
        connections=settings.proxy_connections,
        buffer_bytes=settings.proxy_buffer_bytes,
        idle_s=settings.proxy_idle_s,
        lifetime_s=settings.proxy_lifetime_s,
        connect_s=settings.proxy_connect_s,
    )


def _load_account_settings(account_home: str) -> Settings:
    """Read the optional user file and merge it over the shipped defaults."""
    user_file = Path(user_settings_path(account_home))
    try:
        user_text = user_file.read_text(encoding="utf-8")
    except FileNotFoundError:
        user_text = None
    except OSError as error:
        message = f"settings: cannot read {user_file}: {error}"
        raise SettingsError(message) from error
    return load_settings(
        defaults_text=shipped_defaults_text(), user_text=user_text, account_home=account_home
    )


def _resolve_executable(setting: str, path: str) -> str:
    """Return the worker executable: the configured file, or `claude` found on the search path."""
    if setting == "auto":
        found = shutil.which(WORKER_NAME, path=path)
        if found is None:
            message = (
                f"settings: {EXECUTABLE_KEY}: no {WORKER_NAME} executable found on {path}; "
                "install Claude Code or set this key to its absolute path"
            )
            raise SettingsError(message)
        return found
    candidate = Path(setting)
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        message = f"settings: {EXECUTABLE_KEY}: {setting} is not an executable file"
        raise SettingsError(message)
    return setting
