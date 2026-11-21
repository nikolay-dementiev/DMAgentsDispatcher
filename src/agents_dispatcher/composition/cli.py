"""Composition root of the agentsd command: the only place that builds concrete objects."""

import sys
import time

from agents_dispatcher.adapters.system.claude_provider import ClaudeCodeProvider
from agents_dispatcher.adapters.system.claude_worker import DEFAULT_PATH
from agents_dispatcher.adapters.system.codex_provider import CodexProvider
from agents_dispatcher.adapters.system.decision_log import (
    FileDecisionLog,
    FileQuarantineLog,
    FileResidualLog,
)
from agents_dispatcher.adapters.system.detached_launcher import DetachedSupervisorLauncher
from agents_dispatcher.adapters.system.git_workspace import GitWorkspace
from agents_dispatcher.adapters.system.json_report import JsonReportAmender, JsonReportRenderer
from agents_dispatcher.adapters.system.kept_sandbox import KeptSandbox
from agents_dispatcher.adapters.system.orphan_sweep import OrphanSweeper
from agents_dispatcher.adapters.system.os_free_space import OsFreeSpace
from agents_dispatcher.adapters.system.os_signaller import OsSignaller
from agents_dispatcher.adapters.system.package_version import PackageMetadataVersionSource
from agents_dispatcher.adapters.system.process_census import LibprocCensus
from agents_dispatcher.adapters.system.process_table import LibprocProcessTable
from agents_dispatcher.adapters.system.quarantine_look import QuarantineLook
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
from agents_dispatcher.adapters.system.timestamps import iso_utc
from agents_dispatcher.adapters.system.uuid_ids import UuidRunIds
from agents_dispatcher.application.containment import ContainmentCleanup
from agents_dispatcher.application.quarantine_resolution import ResolveQuarantine, SweepAndRecheck
from agents_dispatcher.application.routing import Router, TaskResolver
from agents_dispatcher.application.run_queries import ReadReport, RunStatus, StopRun, WaitRun
from agents_dispatcher.application.services import Services
from agents_dispatcher.application.settings import Settings
from agents_dispatcher.application.start_run import StartRun
from agents_dispatcher.application.storage_admission import StorageAdmission
from agents_dispatcher.composition.mcp import serve_account
from agents_dispatcher.composition.wiring import (
    CONTROL_DIR,
    STORE_FILE,
    WorkerEnvironment,
    build_account_gates,
    launcher_config,
    live_run_identities,
    load_account_settings,
)
from agents_dispatcher.domain.run import Limits
from agents_dispatcher.interfaces.cli.app import main as run_cli

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
        serve_mcp=lambda: serve_account(build_account_services),
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
    """Wire the use cases of the commands; the router refuses what the supervisor could not run."""
    store = SqliteRunStore(
        f"{settings.store_path}/{STORE_FILE}", epoch_s=time.time, busy_s=settings.sqlite_busy_s
    )
    owners = FileRunOwners(f"{settings.store_path}/{CONTROL_DIR}")
    clock = SuspendInclusiveClock()
    sleeper = SystemSleeper()
    environment = WorkerEnvironment(account_home, account_user, interpreter, path)
    status = RunStatus(reader=store, owner=owners, summary_bytes=settings.public_summary_bytes)
    table = LibprocProcessTable()
    cleanup = ContainmentCleanup(
        LibprocCensus(table), OsSignaller(table), sleeper, settings.observation
    ).run
    protected = live_run_identities(store)
    closer = OrphanSweeper(
        store,
        table,
        owners,
        term_grace_s=settings.term_grace_s,
        kill_wait_s=settings.kill_wait_s,
        unclaimed_after_s=settings.watchdog_startup_s + UNCLAIMED_MARGIN_S,
        run_area=settings.run_area_path,
        containment=cleanup,
        protected=protected,
        notify=write_warning,
        residuals=FileResidualLog(settings.logging_directory, epoch_s=time.time, stamp=iso_utc),
    )
    quarantine = ResolveQuarantine(
        ledger=store,
        look=QuarantineLook(
            store,
            table,
            run_area=settings.run_area_path,
            containment=cleanup,
            protected=protected,
        ),
        workspace=KeptSandbox(settings.run_area_path),
        amender=JsonReportAmender(),
        events=FileQuarantineLog(settings.logging_directory),
        epoch_s=time.time,
        warn=write_warning,
        stamp=iso_utc,
    )
    return Services(
        start=StartRun(
            repository=GitWorkspace(settings.run_area_path),
            ids=UuidRunIds(),
            store=store,
            sweeper=SweepAndRecheck(closer, quarantine),
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
            resolver=TaskResolver(
                settings.tasks,
                default_mode=settings.default_mode,
                default_chain=settings.default_chain,
                allow_write_tasks=settings.allow_write_tasks,
            ),
            capacities={name: item.capacity for name, item in settings.providers.items()},
            storage=StorageAdmission(
                OsFreeSpace(), settings.run_area_path, settings.free_floor_bytes
            ),
            router=Router(
                {
                    "builtin.claude": ClaudeCodeProvider(settings.executables["anthropic"], path),
                    "builtin.codex": CodexProvider(settings.executables["openai"], path),
                },
                settings.providers,
                occupancy=store,
                gates=build_account_gates(
                    settings, environment, store=store, table=LibprocProcessTable()
                ),
                recommendation_model=settings.recommendation_model,
                recommendation_effort=settings.recommendation_effort,
            ),
            decisions=FileDecisionLog(settings.logging_directory),
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
        quarantine=quarantine,
    )
