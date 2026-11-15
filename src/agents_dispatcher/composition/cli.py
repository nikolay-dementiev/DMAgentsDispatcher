"""Composition root of the agentsd command: the only place that builds concrete objects."""

import sys
import time

from agents_dispatcher.adapters.system.claude_provider import resolve_executable
from agents_dispatcher.adapters.system.claude_worker import DEFAULT_PATH
from agents_dispatcher.adapters.system.detached_launcher import DetachedSupervisorLauncher
from agents_dispatcher.adapters.system.git_workspace import GitWorkspace
from agents_dispatcher.adapters.system.json_report import JsonReportRenderer
from agents_dispatcher.adapters.system.orphan_sweep import OrphanSweeper
from agents_dispatcher.adapters.system.package_version import PackageMetadataVersionSource
from agents_dispatcher.adapters.system.process_table import LibprocProcessTable
from agents_dispatcher.adapters.system.run_owner_lock import FileRunOwners
from agents_dispatcher.adapters.system.settings_loader import (
    account_home_dir,
    account_user_name,
    user_settings_path,
)
from agents_dispatcher.adapters.system.signal_interrupts import SignalInterrupts
from agents_dispatcher.adapters.system.sqlite_run_store import SqliteRunStore, write_warning
from agents_dispatcher.adapters.system.system_clock import SuspendInclusiveClock
from agents_dispatcher.adapters.system.system_sleeper import SystemSleeper
from agents_dispatcher.adapters.system.uuid_ids import UuidRunIds
from agents_dispatcher.application.run_queries import ReadReport, RunStatus, StopRun, WaitRun
from agents_dispatcher.application.settings import Settings
from agents_dispatcher.application.start_run import StartRun
from agents_dispatcher.composition.wiring import (
    CONTROL_DIR,
    STORE_FILE,
    WorkerEnvironment,
    launcher_config,
    load_account_settings,
)
from agents_dispatcher.domain.routing import RunRouting, TaskMode
from agents_dispatcher.domain.run import Limits
from agents_dispatcher.interfaces.cli.app import main as run_cli
from agents_dispatcher.interfaces.cli.services import Services

# A run is abandoned this long after the start-up bound: time for a slow `start` to answer.
UNCLAIMED_MARGIN_S = 5.0


def main() -> int:
    """Entry point of the console script."""
    return run_cli(
        sys.argv[1:],
        sys.stdout,
        sys.stderr,
        PackageMetadataVersionSource(),
        build_account_services,
        SignalInterrupts(),
    )


def build_account_services() -> Services:
    """Load the settings of the real account and wire the services; called only for a command."""
    account_home = account_home_dir()
    return build_services(
        load_account_settings(account_home),
        account_home=account_home,
        account_user=account_user_name(),
        interpreter=sys.executable,
        path=DEFAULT_PATH,
        user_settings=user_settings_path(account_home),
    )


def build_services(  # noqa: PLR0913 - one keyword per fact about the account and the interpreter
    settings: Settings,
    *,
    account_home: str,
    account_user: str,
    interpreter: str,
    path: str,
    user_settings: str,
) -> Services:
    """Wire the use cases of the commands; refuse early what the supervisor could not run."""
    # Resolved here too: a worker that is missing must fail `start` visibly, not a supervisor
    # whose output nobody reads.
    resolve_executable(settings.worker_executable, path)
    store = SqliteRunStore(
        f"{settings.store_path}/{STORE_FILE}", epoch_s=time.time, busy_s=settings.sqlite_busy_s
    )
    owners = FileRunOwners(f"{settings.store_path}/{CONTROL_DIR}")
    clock = SuspendInclusiveClock()
    sleeper = SystemSleeper()
    environment = WorkerEnvironment(account_home, account_user, interpreter, path)
    status = RunStatus(reader=store, owner=owners, summary_bytes=settings.public_summary_bytes)
    closer = OrphanSweeper(
        store,
        LibprocProcessTable(),
        owners,
        term_grace_s=settings.term_grace_s,
        kill_wait_s=settings.kill_wait_s,
        unclaimed_after_s=settings.watchdog_startup_s + UNCLAIMED_MARGIN_S,
        notify=write_warning,
    )
    return Services(
        start=StartRun(
            repository=GitWorkspace(settings.run_area_path),
            ids=UuidRunIds(),
            store=store,
            sweeper=closer,
            launcher=DetachedSupervisorLauncher(
                launcher_config(settings, environment, user_settings=user_settings)
            ),
            clock=clock,
            sleeper=sleeper,
            renderer=JsonReportRenderer(),
            default_limits=Limits(hard_s=settings.hard_s, memory_bytes=settings.memory_bytes),
            startup_s=settings.watchdog_startup_s,
            scan_s=settings.watchdog_scan_s,
            summary_bytes=settings.public_summary_bytes,
            input_bytes=settings.input_bytes,
            routing=RunRouting(
                None, TaskMode.WRITE, "anthropic", settings.worker_model, settings.worker_effort, ()
            ),
        ),
        status=status,
        stop=StopRun(stops=store, reason_bytes=settings.public_reason_bytes),
        wait=WaitRun(
            status=status,
            closer=closer,
            clock=clock,
            sleeper=sleeper,
            scan_s=settings.watchdog_scan_s,
        ),
        report=ReadReport(status=status),
    )
