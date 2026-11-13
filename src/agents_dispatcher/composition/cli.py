"""Composition root of the agentsd command: the only place that builds concrete objects."""

import sys
import time

from agents_dispatcher.adapters.system.claude_worker import DEFAULT_PATH
from agents_dispatcher.adapters.system.orphan_sweep import OrphanSweeper
from agents_dispatcher.adapters.system.package_version import PackageMetadataVersionSource
from agents_dispatcher.adapters.system.process_table import LibprocProcessTable
from agents_dispatcher.adapters.system.run_owner_lock import FileRunOwners
from agents_dispatcher.adapters.system.settings_loader import account_home_dir, account_user_name
from agents_dispatcher.adapters.system.sqlite_run_store import SqliteRunStore
from agents_dispatcher.application.run_task import RunTask
from agents_dispatcher.application.settings import Settings
from agents_dispatcher.composition.wiring import (
    CONTROL_DIR,
    STORE_FILE,
    WorkerEnvironment,
    build_run_task,
    load_account_settings,
)
from agents_dispatcher.interfaces.cli.app import main as run_cli


def main() -> int:
    """Entry point of the console script."""
    return run_cli(
        sys.argv[1:], sys.stdout, sys.stderr, PackageMetadataVersionSource(), build_run_service
    )


def build_run_service() -> RunTask:
    """Load the settings of the real account and wire the service; called only for `run`."""
    account_home = account_home_dir()
    return compose_run_service(
        load_account_settings(account_home),
        account_home=account_home,
        account_user=account_user_name(),
        interpreter=sys.executable,
        path=DEFAULT_PATH,
    )


def compose_run_service(
    settings: Settings, *, account_home: str, account_user: str, interpreter: str, path: str
) -> RunTask:
    """Wire every adapter, close the runs an earlier supervisor left behind, return the use case."""
    table = LibprocProcessTable()
    store = SqliteRunStore(
        f"{settings.store_path}/{STORE_FILE}", epoch_s=time.time, busy_s=settings.sqlite_busy_s
    )
    owners = FileRunOwners(f"{settings.store_path}/{CONTROL_DIR}")
    parts = build_run_task(
        settings,
        WorkerEnvironment(account_home, account_user, interpreter, path),
        store=store,
        table=table,
        owners=owners,
    )
    OrphanSweeper(
        store, table, owners, term_grace_s=settings.term_grace_s, kill_wait_s=settings.kill_wait_s
    ).sweep()
    return parts.task
