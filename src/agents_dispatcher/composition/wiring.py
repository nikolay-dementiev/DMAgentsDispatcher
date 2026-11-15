"""Shared wiring of the CLI and the supervisor: settings, confinement paths and the use case."""

from dataclasses import dataclass
from pathlib import Path

import agents_dispatcher
from agents_dispatcher.adapters.system.claude_worker import ClaudeCodeWorker
from agents_dispatcher.adapters.system.detached_launcher import DetachedLauncherConfig
from agents_dispatcher.adapters.system.egress_proxy import ConnectAllowlistProxy, ProxyConfig
from agents_dispatcher.adapters.system.git_workspace import GitWorkspace
from agents_dispatcher.adapters.system.json_report import JsonReportRenderer
from agents_dispatcher.adapters.system.process_table import LibprocProcessTable
from agents_dispatcher.adapters.system.run_owner_lock import FileRunOwners
from agents_dispatcher.adapters.system.seatbelt_confinement import (
    ConfinementConfig,
    SeatbeltConfinement,
)
from agents_dispatcher.adapters.system.seatbelt_profile import (
    CLAUDE_CODE,
    ProviderProfile,
    codex_profile,
)
from agents_dispatcher.adapters.system.settings_loader import (
    load_settings,
    shipped_defaults_text,
    user_settings_path,
)
from agents_dispatcher.adapters.system.sqlite_run_store import SqliteRunStore
from agents_dispatcher.adapters.system.system_clock import SuspendInclusiveClock
from agents_dispatcher.adapters.system.tool_lookup import resolve_tool
from agents_dispatcher.application.ports import StopPoll
from agents_dispatcher.application.run_task import RunTask
from agents_dispatcher.application.settings import Settings, SettingsError
from agents_dispatcher.domain.run import Limits

TOOL_NAMES = {"builtin.claude": "claude", "builtin.codex": "codex"}
STORE_FILE = "runs.sqlite3"
CONTROL_DIR = "control"
# The supervisor is started from the package copy that runs `start` (amendment A5 of PLAN-5), so a
# copied or uninstalled tree supervises its own runs with its own code.
PACKAGE_ROOT = Path(agents_dispatcher.__file__).resolve().parents[1]
BOOTSTRAP_RELATIVE = "agents_dispatcher/composition/supervisor_bootstrap.py"
BRIEF_WRITE_TIMEOUT_S = 5.0


@dataclass(frozen=True)
class WorkerEnvironment:
    """The account and the executables a run needs; none of it is secret."""

    account_home: str
    account_user: str
    interpreter: str
    path: str


@dataclass(frozen=True)
class RunParts:
    """The use case with the concrete objects the caller still needs afterwards."""

    task: RunTask
    proxy: ConnectAllowlistProxy


def build_run_task(  # noqa: PLR0913 - the composition root wires exactly these collaborators
    settings: Settings,
    environment: WorkerEnvironment,
    *,
    store: SqliteRunStore,
    table: LibprocProcessTable,
    owners: FileRunOwners,
    stops: StopPoll,
    provider: str,
) -> RunParts:
    """Wire every adapter of one run into the `RunTask` use case."""
    executable = resolve_tool(
        settings.executables[provider],
        environment.path,
        name=TOOL_NAMES[settings.providers[provider].implementation],
        key=f"profiles.{provider}.main.executable",
    )
    clock = SuspendInclusiveClock()
    proxy = ConnectAllowlistProxy(proxy_config(settings, provider))
    task = RunTask(
        workspace=GitWorkspace(settings.run_area_path),
        egress=proxy,
        worker=ClaudeCodeWorker(
            executable=executable,
            home=environment.account_home,
            user=environment.account_user,
            kept_bytes=settings.public_summary_bytes,
            path=environment.path,
        ),
        confinement=SeatbeltConfinement(
            ConfinementConfig(
                denied_reads=denied_read_paths(settings, provider),
                term_grace_s=settings.term_grace_s,
                kill_wait_s=settings.kill_wait_s,
                scan_s=settings.watchdog_scan_s,
                interpreter=environment.interpreter,
                provider=provider_profile(settings, provider),
            ),
            table=table,
            clock=clock,
        ),
        store=store,
        clock=clock,
        owners=owners,
        stops=stops,
        renderer=JsonReportRenderer(),
        default_limits=Limits(hard_s=settings.hard_s, memory_bytes=settings.memory_bytes),
    )
    return RunParts(task, proxy)


def launcher_config(
    settings: Settings, environment: WorkerEnvironment, *, user_settings: str
) -> DetachedLauncherConfig:
    """Describe how `start` launches a supervisor: this package's bootstrap, this account."""
    return DetachedLauncherConfig(
        interpreter=environment.interpreter,
        bootstrap=str(PACKAGE_ROOT / BOOTSTRAP_RELATIVE),
        leading_args=(str(PACKAGE_ROOT),),
        account_home=environment.account_home,
        account_user=environment.account_user,
        path=environment.path,
        store_path=settings.store_path,
        user_settings=user_settings,
        write_timeout_s=BRIEF_WRITE_TIMEOUT_S,
    )


def provider_profile(settings: Settings, provider: str) -> ProviderProfile:
    """Return the confinement profile of the implementation that runs `provider`."""
    implementation = settings.providers[provider].implementation
    profiles = {"builtin.claude": CLAUDE_CODE, "builtin.codex": codex_profile()}
    if implementation not in profiles:
        message = f"settings: providers.{provider}.implementation: {implementation} has no profile"
        raise SettingsError(message)
    return profiles[implementation]


def denied_read_paths(settings: Settings, provider: str) -> tuple[str, ...]:
    """Expand the `sandbox.read_deny` selectors into the paths the profile denies reading.

    `@other-vendor-state` is every tool's login state but the one of `provider` itself, which its
    own worker must read to log in.
    """
    others = tuple(
        path
        for owner, paths in settings.vendor_state.items()
        if owner != provider
        for path in paths
    )
    expansion = {
        "@sensitive-user-data": settings.sensitive_user_data,
        "@other-vendor-state": others + settings.other_vendor_state,
        "@sdk-control": (settings.store_path, settings.logging_directory),
        "@other-runs": (settings.run_area_path,),
    }
    return tuple(path for selector in settings.read_deny for path in expansion[selector])


def proxy_config(settings: Settings, provider: str) -> ProxyConfig:
    """Map the proxy settings and the hosts of `provider` onto the proxy's own configuration."""
    return ProxyConfig(
        hosts=settings.provider_hosts[provider],
        ports=settings.proxy_ports,
        allow_nonpublic=settings.proxy_allow_nonpublic,
        header_bytes=settings.proxy_header_bytes,
        connections=settings.proxy_connections,
        buffer_bytes=settings.proxy_buffer_bytes,
        idle_s=settings.proxy_idle_s,
        lifetime_s=settings.proxy_lifetime_s,
        connect_s=settings.proxy_connect_s,
    )


def load_account_settings(account_home: str, user_file: str | None = None) -> Settings:
    """Read the optional user file and merge it over the shipped defaults."""
    location = Path(user_file or user_settings_path(account_home))
    try:
        user_text = location.read_text(encoding="utf-8")
    except FileNotFoundError:
        user_text = None
    except OSError as error:
        message = f"settings: cannot read {location}: {error}"
        raise SettingsError(message) from error
    return load_settings(
        defaults_text=shipped_defaults_text(), user_text=user_text, account_home=account_home
    )
