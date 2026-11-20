"""Shared wiring of the CLI and the supervisor: settings, confinement paths and the use case."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import agents_dispatcher
from agents_dispatcher.adapters.system.account_fingerprint import fingerprint
from agents_dispatcher.adapters.system.account_observers import (
    BoundLoginObserver,
    ConfinedAccountReader,
)
from agents_dispatcher.adapters.system.claude_worker import ClaudeCodeWorker
from agents_dispatcher.adapters.system.codex_account import BOUND_BUCKET, CodexAccountDialogue
from agents_dispatcher.adapters.system.codex_worker import CodexWorker
from agents_dispatcher.adapters.system.decision_log import FileInspectionLog
from agents_dispatcher.adapters.system.detached_launcher import DetachedLauncherConfig
from agents_dispatcher.adapters.system.egress_proxy import ConnectAllowlistProxy, ProxyConfig
from agents_dispatcher.adapters.system.git_workspace import GitWorkspace
from agents_dispatcher.adapters.system.json_report import JsonReportRenderer
from agents_dispatcher.adapters.system.login_inspector import ConfinedLoginInspector, LoginProbe
from agents_dispatcher.adapters.system.login_probes import ClaudeLoginProbe, CodexLoginProbe
from agents_dispatcher.adapters.system.os_signaller import OsSignaller
from agents_dispatcher.adapters.system.process_census import LibprocCensus
from agents_dispatcher.adapters.system.process_table import LibprocProcessTable
from agents_dispatcher.adapters.system.run_owner_lock import FileRunOwners
from agents_dispatcher.adapters.system.seatbelt_confinement import (
    ConfinementConfig,
    SeatbeltConfinement,
)
from agents_dispatcher.adapters.system.seatbelt_profile import (
    CLAUDE_CODE,
    ProviderProfile,
    VendorWrite,
    codex_profile,
)
from agents_dispatcher.adapters.system.settings_loader import (
    load_settings,
    shipped_defaults_text,
    user_settings_path,
)
from agents_dispatcher.adapters.system.sqlite_run_store import SqliteRunStore
from agents_dispatcher.adapters.system.system_clock import SuspendInclusiveClock
from agents_dispatcher.adapters.system.system_sleeper import SystemSleeper
from agents_dispatcher.adapters.system.tool_lookup import resolve_tool
from agents_dispatcher.adapters.system.uuid_ids import UuidRunIds
from agents_dispatcher.application.account_gates import AccountReader, ProviderAccountGate
from agents_dispatcher.application.account_watch import ProviderAccountWatch
from agents_dispatcher.application.ports import StopPoll, Worker
from agents_dispatcher.application.routing import AccountGate
from agents_dispatcher.application.run_task import RunTask
from agents_dispatcher.application.settings import Settings, SettingsError
from agents_dispatcher.domain.process import ProcessKey
from agents_dispatcher.domain.quota import QuotaMode
from agents_dispatcher.domain.run import Limits

CODEX_INSTALLATION_ID = "installation_id"
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
        worker=build_worker(settings, environment, provider, executable),
        confinement=SeatbeltConfinement(
            ConfinementConfig(
                denied_reads=denied_read_paths(settings, provider),
                term_grace_s=settings.term_grace_s,
                kill_wait_s=settings.kill_wait_s,
                scan_s=settings.watchdog_scan_s,
                interpreter=environment.interpreter,
                provider=provider_profile(settings, provider),
                observation=settings.observation,
            ),
            table=table,
            clock=clock,
            census=LibprocCensus(table),
            signaller=OsSignaller(table),
            sleeper=SystemSleeper(),
            protected=live_run_identities(store),
        ),
        store=store,
        clock=clock,
        owners=owners,
        stops=stops,
        renderer=JsonReportRenderer(),
        default_limits=Limits(hard_s=settings.hard_s, memory_bytes=settings.memory_bytes),
        watch=_watch(settings, environment, provider, executable, store, table),
    )
    return RunParts(task, proxy)


def build_worker(
    settings: Settings, environment: WorkerEnvironment, provider: str, executable: str
) -> Worker:
    """Return the worker adapter of the implementation that runs `provider`."""
    implementation = settings.providers[provider].implementation
    adapters: dict[str, type[ClaudeCodeWorker | CodexWorker]] = {
        "builtin.claude": ClaudeCodeWorker,
        "builtin.codex": CodexWorker,
    }
    if implementation not in adapters:
        message = f"settings: providers.{provider}.implementation: {implementation} has no worker"
        raise SettingsError(message)
    return adapters[implementation](
        executable=executable,
        home=environment.account_home,
        user=environment.account_user,
        kept_bytes=settings.public_summary_bytes,
        path=environment.path,
    )


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
    if implementation == "builtin.claude":
        return CLAUDE_CODE
    if implementation == "builtin.codex":
        (login_state, *_) = settings.vendor_state[provider]
        return codex_profile((VendorWrite("literal", f"{login_state}/{CODEX_INSTALLATION_ID}"),))
    message = f"settings: providers.{provider}.implementation: {implementation} has no profile"
    raise SettingsError(message)


def inspector_profile(settings: Settings, provider: str) -> ProviderProfile:
    """Return the profile of the account inspector of `provider`.

    The Codex app-server keeps SQLite state next to the login in the real Codex home, so only its
    inspector may also write the `*.sqlite*` files there (MEASURE-9 finding 2, decision D-4). The
    worker profile never gets the pattern.
    """
    profile = provider_profile(settings, provider)
    if settings.providers[provider].implementation != "builtin.codex":
        return profile
    (home, *_) = settings.vendor_state[provider]
    pattern = "^" + _regex_escape(home) + "/[^/]*" + _regex_escape(".sqlite") + "[^/]*$"
    return ProviderProfile(
        profile.mach_services, (*profile.vendor_writes, VendorWrite("regex", pattern))
    )


def _regex_escape(text: str) -> str:
    """Escape the characters that are special in a profile regular expression."""
    return "".join(f"\\{char}" if char in ".^$*+?()[]{}|\\" else char for char in text)


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


def live_run_identities(store: SqliteRunStore) -> Callable[[str], frozenset[ProcessKey]]:
    """Return a function that names the processes of every other unfinished run (A6).

    Their supervisor and shim leader are never attributed to the run being cleaned up, even when
    their working directory or open files lie inside its folder.
    """

    def identities(marker: str) -> frozenset[ProcessKey]:
        keys: set[ProcessKey] = set()
        for run in store.unfinished():
            if run.run_id.value == marker:
                continue
            if run.supervisor_pid is not None and run.supervisor_start_time is not None:
                keys.add(ProcessKey(run.supervisor_pid, run.supervisor_start_time))
            if run.pgid is not None and run.pg_start_time is not None:
                keys.add(ProcessKey(run.pgid, run.pg_start_time))
        return frozenset(keys)

    return identities


def _inspector(
    settings: Settings,
    environment: WorkerEnvironment,
    provider: str,
    profile: ProviderProfile,
    table: LibprocProcessTable,
) -> ConfinedLoginInspector:
    """Return an inspector that runs commands of `provider` under `profile`."""
    confinement = SeatbeltConfinement(
        ConfinementConfig(
            denied_reads=denied_read_paths(settings, provider),
            term_grace_s=settings.term_grace_s,
            kill_wait_s=settings.kill_wait_s,
            scan_s=settings.watchdog_scan_s,
            interpreter=environment.interpreter,
            provider=profile,
            observation=settings.observation,
        ),
        table=table,
        clock=SuspendInclusiveClock(),
        census=LibprocCensus(table),
        signaller=OsSignaller(table),
        sleeper=SystemSleeper(),
        # The inspector works in its own scratch folder under a fresh marker: no other run's
        # process can match it by group, ancestry, marker or folder.
        protected=lambda _marker: frozenset(),
    )
    return ConfinedLoginInspector(
        confinement=confinement,
        ids=UuidRunIds(),
        scratch_root=f"{settings.run_area_path}/inspect",
        limits=Limits(
            hard_s=settings.inspector_timeout_s, memory_bytes=settings.inspector_memory_bytes
        ),
        home=environment.account_home,
        user=environment.account_user,
        path=environment.path,
    )


def build_account_gates(
    settings: Settings,
    environment: WorkerEnvironment,
    *,
    store: SqliteRunStore,
    table: LibprocProcessTable,
) -> dict[str, AccountGate]:
    """Build the account gate of each provider whose tool can be found.

    A provider whose tool is missing has no gate: its plugin refuses the link before any gate is
    asked, and a missing tool must not stop the other providers from being routed.
    """
    gates: dict[str, AccountGate] = {}
    for provider, item in settings.providers.items():
        try:
            executable = resolve_tool(
                settings.executables[provider],
                environment.path,
                name=TOOL_NAMES[item.implementation],
                key=f"profiles.{provider}.main.executable",
            )
        except SettingsError:
            continue
        login, account = _observers(settings, environment, provider, executable, table)
        gates[provider] = ProviderAccountGate(
            login=login,
            account=account,
            marks=store,
            mode=QuotaMode(settings.quota_mode),
            reserve_percent=settings.quota_reserve_percent,
            max_age_s=settings.quota_max_age_s,
            fingerprint=fingerprint,
        )
    return gates


def _observers(
    settings: Settings,
    environment: WorkerEnvironment,
    provider: str,
    executable: str,
    table: LibprocProcessTable,
) -> tuple[BoundLoginObserver, AccountReader | None]:
    """Return the confined login observer of `provider` and, if its tool has one, its reader."""
    implementation = settings.providers[provider].implementation
    log = FileInspectionLog(settings.logging_directory)
    probe: LoginProbe
    account: AccountReader | None = None
    if implementation == "builtin.codex":
        probe = CodexLoginProbe(executable)
        account = ConfinedAccountReader(
            _inspector(
                settings, environment, provider, inspector_profile(settings, provider), table
            ),
            CodexAccountDialogue(executable, settings.bound_buckets.get(provider, BOUND_BUCKET)),
            lambda: ConnectAllowlistProxy(proxy_config(settings, provider)),
            provider,
            log,
        )
    else:
        probe = ClaudeLoginProbe(executable)
    login = BoundLoginObserver(
        _inspector(settings, environment, provider, provider_profile(settings, provider), table),
        probe,
        provider,
        log,
    )
    return login, account


def _watch(  # noqa: PLR0913, PLR0917 - the composition root wires exactly these
    settings: Settings,
    environment: WorkerEnvironment,
    provider: str,
    executable: str,
    store: SqliteRunStore,
    table: LibprocProcessTable,
) -> ProviderAccountWatch:
    """Return the watch over the account of `provider` for one supervised run."""
    login, account = _observers(settings, environment, provider, executable, table)
    return ProviderAccountWatch(login=login, account=account, ledger=store, fingerprint=fingerprint)
