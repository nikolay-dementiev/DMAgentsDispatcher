"""Loads the shipped defaults and the optional user file into resolved `Settings`.

The defaults text is the single source of every default value (ARCH section 3.1). A user
key that the defaults do not know is refused, so a typo can never be silently ignored.
"""

import os
import pwd
import re
import tomllib
from collections.abc import Callable, Mapping
from importlib import resources
from typing import Any

from agents_dispatcher.application.settings import (
    InvalidSettingsError,
    McpSettings,
    ObservationSettings,
    ProviderSettings,
    Settings,
    SettingsError,
    TaskDefinition,
)
from agents_dispatcher.domain.routing import (
    Chain,
    HandoffLink,
    Link,
    ProviderLink,
    TaskMode,
    validate_name,
)

SCHEMA_VERSION = 1
SELECTORS = frozenset(
    {"@sensitive-user-data", "@other-vendor-state", "@sdk-control", "@other-runs"}
)
MAX_PORT = 65535
MAX_RESERVE = 99
_SECONDS = re.compile(r"\d+(\.\d{1,9})?")
_MODEL = re.compile(r"[^\s\x00-\x1f\x7f-\x9f-][^\s\x00-\x1f\x7f-\x9f]*")
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
_TOKEN = re.compile(r"(@[a-z-]+)(/.*)?")
_PROVIDER_KEY = re.compile(r"providers\.([^.]+)\.(enabled|implementation|model|effort|capacity)")
_TEMPLATE = "_template"
_IMPLEMENTATION = re.compile(r"[a-z][a-z0-9.-]*")
_HOSTS_KEY = re.compile(r"proxy\.providers\.([^.]+)\.hosts")
_BUCKET_KEY = re.compile(r"profiles\.([^.]+)\.main\.bound_bucket")
_EXECUTABLE_KEY = re.compile(r"profiles\.([^.]+)\.main\.executable")
_VENDOR_STATE_KEY = re.compile(r"sandbox\.vendor_state\.([^.]+)")
_BUCKET = re.compile(r"[a-z][a-z0-9_-]{0,63}")
QUOTA_MODES = ("required", "best_effort", "off")
MCP_TRANSPORTS = ("own", "sdk")
_TASK_KEY = re.compile(r"tasks\.([^.]+)\.(mode|chain)")

Validator = Callable[[str, object], object]


def shipped_defaults_text() -> str:
    """Return the text of the defaults file that ships inside the package."""
    return (
        resources.files("agents_dispatcher.adapters.system")
        .joinpath("defaults.toml")
        .read_text(encoding="utf-8")
    )


def account_home_dir() -> str:
    """Return the home directory recorded for the real user id; the environment is ignored."""
    home = pwd.getpwuid(os.getuid()).pw_dir
    if not home.startswith("/"):
        message = f"settings: account home: recorded home {home!r} is not absolute"
        raise SettingsError(message)
    return home


def account_user_name() -> str:
    """Return the account name recorded for the real user id; the environment is ignored."""
    return pwd.getpwuid(os.getuid()).pw_name


def user_settings_path(account_home: str) -> str:
    """Return where the optional user settings file lives (ARCH section 3.1)."""
    return f"{account_home}/.config/agents-dispatcher/settings.toml"


def load_settings(*, defaults_text: str, user_text: str | None, account_home: str) -> Settings:
    """Merge the user file over the defaults, validate every key and resolve the locations."""
    values = _flatten(_parse(defaults_text, "defaults"))
    declared = _declared_providers(values)
    if user_text is not None:
        for key, value in _flatten(_parse(user_text, "user file")).items():
            if key not in values and not _is_open_key(key, declared):
                raise _error(key, "unknown key")
            values[key] = value
    missing = sorted(set(_VALIDATORS) - set(values))
    if missing:
        raise _error(missing[0], "missing from the defaults")
    checked = {key: check(key, values[key]) for key, check in _VALIDATORS.items()}
    store = _resolve(account_home, None, "store.path", str(checked["store.path"]))
    tasks = _tasks(values, declared)
    providers = _providers(values, declared)
    return Settings(
        store_path=store,
        run_area_path=_resolve(account_home, store, "run_area.path", str(checked["run_area.path"])),
        memory_bytes=int(str(checked["resources.run.memory_bytes"])),
        file_bytes=int(str(checked["resources.run.file_bytes"])),
        descriptors_per_process=int(str(checked["resources.run.descriptors_per_process"])),
        aggregate_descriptors=int(str(checked["resources.run.aggregate_descriptors"])),
        output_bytes=int(str(checked["resources.capture.output_bytes"])),
        hard_s=float(str(checked["watchdog.hard_s"])),
        term_grace_s=float(str(checked["watchdog.term_grace_s"])),
        kill_wait_s=float(str(checked["watchdog.kill_wait_s"])),
        watchdog_scan_s=float(str(checked["watchdog.scan_s"])),
        watchdog_startup_s=float(str(checked["watchdog.startup_s"])),
        public_reason_bytes=int(str(checked["public.reason_bytes"])),
        public_summary_bytes=int(str(checked["public.summary_bytes"])),
        logging_directory=_resolve(
            account_home, store, "logging.directory", str(checked["logging.directory"])
        ),
        provider_hosts=_provider_hosts(checked),
        proxy_ports=tuple(_ints(checked["proxy.ports"])),
        read_deny=tuple(_strings(checked["sandbox.read_deny"])),
        sensitive_user_data=_resolve_all(
            account_home, store, "sandbox.sensitive_user_data", checked
        ),
        other_vendor_state=_resolve_all(account_home, store, "sandbox.other_vendor_state", checked),
        vendor_state=_vendor_state(account_home, store, checked),
        input_bytes=int(str(checked["security.input_bytes"])),
        executables=_executables(values, declared),
        sqlite_busy_s=float(str(checked["storage.sqlite_busy_s"])),
        inspector_timeout_s=float(str(checked["inspector.timeout_s"])),
        inspector_memory_bytes=int(str(checked["inspector.memory_bytes"])),
        mcp=_mcp(checked),
        observation=_observation(checked),
        proxy_allow_nonpublic=checked["proxy.allow_nonpublic"] is True,
        proxy_header_bytes=int(str(checked["proxy.header_bytes"])),
        proxy_connections=int(str(checked["proxy.connections"])),
        proxy_buffer_bytes=int(str(checked["proxy.buffer_bytes"])),
        proxy_idle_s=float(str(checked["proxy.idle_s"])),
        proxy_lifetime_s=float(str(checked["proxy.lifetime_s"])),
        proxy_connect_s=float(str(checked["proxy.connect_s"])),
        default_mode=TaskMode(str(checked["defaults.mode"])),
        recommendation_model=str(checked["defaults.recommendation_model"]),
        recommendation_effort=str(checked["defaults.recommendation_effort"]),
        quota_mode=str(checked["defaults.quota_mode"]),
        quota_reserve_percent=int(str(checked["defaults.quota_reserve_percent"])),
        quota_max_age_s=float(str(checked["defaults.quota_max_age_s"])),
        bound_buckets=_bound_buckets(values),
        tasks=tasks,
        allow_write_tasks=_allowed_writes(checked["security.allow_write_tasks"], tasks),
        providers=providers,
        default_chain=_chain("defaults.chain", values["defaults.chain"], declared),
    )


def _observation(checked: Mapping[str, object]) -> ObservationSettings:
    """Build the observation settings; a bound that could never be met is refused."""
    prefix = "process.observation."
    process_interval = float(str(checked[prefix + "process_interval_s"]))
    process_age = float(str(checked[prefix + "process_max_age_s"]))
    path_interval = float(str(checked[prefix + "path_interval_s"]))
    path_age = float(str(checked[prefix + "path_max_age_s"]))
    empty = int(str(checked[prefix + "empty_rounds"]))
    rounds = int(str(checked[prefix + "cleanup_rounds"]))
    if process_age < process_interval:
        raise _error(prefix + "process_max_age_s", "must not be below process_interval_s")
    if path_age < path_interval:
        raise _error(prefix + "path_max_age_s", "must not be below path_interval_s")
    if rounds < empty:
        raise _error(prefix + "cleanup_rounds", "must not be below empty_rounds")
    return ObservationSettings(
        process_interval_s=process_interval,
        process_max_age_s=process_age,
        path_interval_s=path_interval,
        path_max_age_s=path_age,
        empty_rounds=empty,
        cleanup_rounds=rounds,
    )


def _mcp(checked: Mapping[str, object]) -> McpSettings:
    """Build the MCP server settings; the drain is part of the total shutdown time."""
    drain = float(str(checked["mcp.shutdown_drain_s"]))
    total = float(str(checked["mcp.shutdown_total_s"]))
    if drain > total:
        key = "mcp.shutdown_drain_s"
        raise _error(key, "must not exceed mcp.shutdown_total_s")
    return McpSettings(
        transport=str(checked["mcp.transport"]),
        frame_in_bytes=int(str(checked["mcp.frame.in_bytes"])),
        frame_out_bytes=int(str(checked["mcp.frame.out_bytes"])),
        frame_depth=int(str(checked["mcp.frame.depth"])),
        frame_nodes=int(str(checked["mcp.frame.nodes"])),
        numeric_token_bytes=int(str(checked["mcp.frame.numeric_token_bytes"])),
        ids_maximum=int(str(checked["mcp.ids.maximum"])),
        string_bytes=int(str(checked["mcp.ids.string_bytes"])),
        normal_workers=int(str(checked["mcp.normal.workers"])),
        normal_queue=int(str(checked["mcp.normal.queue"])),
        control_workers=int(str(checked["mcp.control.workers"])),
        control_queue=int(str(checked["mcp.control.queue"])),
        initialize_timeout_s=float(str(checked["mcp.initialize_timeout_s"])),
        partial_frame_timeout_s=float(str(checked["mcp.partial_frame_timeout_s"])),
        write_timeout_s=float(str(checked["mcp.write_timeout_s"])),
        shutdown_drain_s=drain,
        shutdown_total_s=total,
        priority_burst=int(str(checked["mcp.priority_burst"])),
        wait_max_s=float(str(checked["mcp.wait_max_s"])),
        aggregate_bytes=int(str(checked["mcp.input.aggregate_bytes"])),
    )


def _error(key: str, problem: str) -> InvalidSettingsError:
    """Build the error that names the key."""
    return InvalidSettingsError(f"settings: {key}: {problem}")


def _parse(text: str, source: str) -> dict[str, Any]:
    """Parse TOML text; a syntax error becomes a settings error."""
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        message = f"settings: cannot parse the {source}: {error}"
        raise InvalidSettingsError(message) from error


def _declared_providers(values: Mapping[str, object]) -> list[str]:
    """Return the provider ids the shipped defaults declare, in order; the template is no id."""
    ids: list[str] = []
    for key in values:
        match = _PROVIDER_KEY.fullmatch(key)
        if match is not None and match.group(1) != _TEMPLATE and match.group(1) not in ids:
            ids.append(match.group(1))
    return ids


def _is_open_key(key: str, declared: list[str]) -> bool:
    """Return whether a user key that the defaults lack is still allowed: a task or a provider."""
    if _TASK_KEY.fullmatch(key) is not None:
        return True
    match = _PROVIDER_KEY.fullmatch(key) or _EXECUTABLE_KEY.fullmatch(key)
    return match is not None and match.group(1) in declared


def _providers(values: Mapping[str, object], declared: list[str]) -> dict[str, ProviderSettings]:
    """Build each declared provider; unset enabled and effort come from the template."""
    providers: dict[str, ProviderSettings] = {}
    for name in declared:
        providers[name] = ProviderSettings(
            enabled=_provider_field(values, name, "enabled", _boolean) is True,
            implementation=str(_provider_field(values, name, "implementation", _implementation)),
            model=str(_provider_field(values, name, "model", _model)),
            effort=str(_provider_field(values, name, "effort", _effort)),
            capacity=int(str(_provider_field(values, name, "capacity", _positive_int))),
        )
    return providers


def _provider_hosts(checked: Mapping[str, object]) -> dict[str, tuple[str, ...]]:
    """Return the allowed proxy hosts of each provider the settings list."""
    return {
        match.group(1): tuple(_strings(value))
        for key, value in checked.items()
        if (match := _HOSTS_KEY.fullmatch(key)) is not None
    }


def _vendor_state(
    account_home: str, store: str, checked: Mapping[str, object]
) -> dict[str, tuple[str, ...]]:
    """Return the login-state locations of each provider the defaults list."""
    return {
        match.group(1): _resolve_all(account_home, store, key, checked)
        for key in checked
        if (match := _VENDOR_STATE_KEY.fullmatch(key)) is not None
    }


def _executables(values: Mapping[str, object], declared: list[str]) -> dict[str, str]:
    """Return each provider's tool setting: its own key, else the template's, each checked."""
    result: dict[str, str] = {}
    for name in declared:
        key = f"profiles.{name}.main.executable"
        source = key if key in values else "profiles._template.executable"
        result[name] = str(_executable(key, values[source]))
    return result


def _bound_buckets(values: Mapping[str, object]) -> dict[str, str]:
    """Return the quota bucket of each provider's main profile, each value checked."""
    return {
        match.group(1): str(_bucket(key, value))
        for key, value in values.items()
        if (match := _BUCKET_KEY.fullmatch(key)) is not None
    }


def _provider_field(
    values: Mapping[str, object], name: str, field: str, check: Validator
) -> object:
    """Return one checked provider value: its own, else the template's, else a refusal."""
    key = f"providers.{name}.{field}"
    if key in values:
        return check(key, values[key])
    inherited = f"providers.{_TEMPLATE}.{field}"
    if inherited in values:
        return check(key, values[inherited])
    raise _error(key, "required: every provider declares it")


def _chain(key: str, value: object, declared: list[str]) -> Chain:
    """Parse a list of link tables into a validated chain; every problem names the key."""
    if not isinstance(value, list):
        raise _error(key, 'must be a list of links such as { provider = "anthropic" }')
    links: list[Link] = []
    for number, item in enumerate(value, start=1):
        links.append(_link(key, number, item, declared))
    try:
        return Chain(tuple(links))
    except ValueError as problem:
        raise _error(key, str(problem)) from problem


def _link(key: str, number: int, item: object, declared: list[str]) -> Link:
    """Parse one link table: a provider with optional model and effort, or the handoff."""
    where = f"{key}[{number}]"
    if not isinstance(item, dict):
        raise _error(key, f"link {number} must be a table")
    if set(item) == {"handoff"}:
        if item["handoff"] is not True:
            handoff_key = f"{where}.handoff"
            raise _error(handoff_key, "must be true")
        return HandoffLink()
    unknown = sorted(set(item) - {"provider", "model", "effort"})
    if unknown or "provider" not in item:
        problem = f"has unknown key {unknown[0]!r}" if unknown else "needs a provider or handoff"
        raise _error(key, f"link {number} {problem}")
    name = item["provider"]
    if name not in declared:
        raise _error(key, f"link {number}: provider {name!r} is not declared in providers")
    model = _model(f"{where}.model", item["model"]) if "model" in item else None
    effort = _effort(f"{where}.effort", item["effort"]) if "effort" in item else None
    try:
        return ProviderLink(str(name), model=_optional_text(model), effort=_optional_text(effort))
    except ValueError as problem:
        raise _error(key, f"link {number}: {problem}") from problem


def _optional_text(value: object) -> str | None:
    """Narrow a validated optional value to text."""
    return None if value is None else str(value)


def _tasks(values: Mapping[str, object], declared: list[str]) -> dict[str, TaskDefinition]:
    """Build the task types the merged settings declare; each needs a valid name and a mode."""
    names: list[str] = []
    for key in values:
        match = _TASK_KEY.fullmatch(key)
        if match is not None and match.group(1) not in names:
            names.append(match.group(1))
    tasks: dict[str, TaskDefinition] = {}
    for name in names:
        try:
            validate_name(name, "task type")
        except ValueError as problem:
            key = f"tasks.{name}"
            raise _error(key, str(problem)) from problem
        mode_key = f"tasks.{name}.mode"
        if mode_key not in values:
            raise _error(mode_key, "required: every task type declares its mode")
        chain_key = f"tasks.{name}.chain"
        tasks[name] = TaskDefinition(
            mode=TaskMode(str(_mode(mode_key, values[mode_key]))),
            chain=_chain(chain_key, values[chain_key], declared) if chain_key in values else None,
        )
    return tasks


def _allowed_writes(value: object, tasks: Mapping[str, TaskDefinition]) -> tuple[str, ...]:
    """Return the write allow-list; every entry must name a declared task type."""
    key = "security.allow_write_tasks"
    names = tuple(_strings(value))
    for name in names:
        if name not in tasks:
            raise _error(key, f"{name!r} is not a declared task type")
    return names


def _flatten(table: Mapping[str, Any], prefix: str = "") -> dict[str, object]:
    """Turn nested tables into dotted keys; lists and scalars are leaves."""
    flat: dict[str, object] = {}
    for name, value in table.items():
        key = f"{prefix}{name}"
        if isinstance(value, dict):
            flat.update(_flatten(value, f"{key}."))
        else:
            flat[key] = value
    return flat


def _resolve(account_home: str, store: str | None, key: str, value: str) -> str:
    """Expand a leading token into an absolute path, or accept an absolute path."""
    match = _TOKEN.fullmatch(value)
    if match is None:
        if not value.startswith("/"):
            raise _error(key, f"{value!r} is neither an absolute path nor a token path")
        return _checked_path(key, value)
    root_name, rest = match.group(1), match.group(2) or ""
    roots = {
        "@account-home": account_home,
        "@account-config": f"{account_home}/.config",
        "@account-state": f"{account_home}/.local/state",
    }
    if root_name == "@state" and store is not None:
        roots["@state"] = store
    if root_name not in roots:
        raise _error(key, f"unknown or unavailable token {root_name}")
    return _checked_path(key, roots[root_name] + rest)


def _checked_path(key: str, path: str) -> str:
    """Refuse parent-directory segments so a path cannot leave its root."""
    if ".." in path.split("/"):
        raise _error(key, f"{path!r} contains a '..' segment")
    return path.rstrip("/") or "/"


def _resolve_all(
    account_home: str, store: str, key: str, checked: Mapping[str, object]
) -> tuple[str, ...]:
    """Resolve every entry of a path-list key."""
    return tuple(_resolve(account_home, store, key, item) for item in _strings(checked[key]))


def _strings(value: object) -> list[str]:
    """Narrow a validated value to a list of strings."""
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _ints(value: object) -> list[int]:
    """Narrow a validated value to a list of integers."""
    return [item for item in value if isinstance(item, int)] if isinstance(value, list) else []


def _is_int(value: object) -> bool:
    """Return True for an integer that is not a boolean."""
    return isinstance(value, int) and not isinstance(value, bool)


def _schema_version(key: str, value: object) -> object:
    """Accept only the exact schema constant."""
    if not _is_int(value) or value != SCHEMA_VERSION:
        raise _error(key, f"must be exactly {SCHEMA_VERSION}")
    return value


def _positive_int(key: str, value: object) -> object:
    """Accept a positive integer."""
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise _error(key, "must be a positive integer")
    return value


def _seconds(key: str, value: object) -> object:
    """Accept quoted positive decimal seconds with at most nine fraction digits."""
    if not isinstance(value, str) or _SECONDS.fullmatch(value) is None or float(value) <= 0:
        raise _error(key, "must be a quoted positive decimal with at most nine fraction digits")
    return value


def _path(key: str, value: object) -> object:
    """Accept a non-empty string; resolution checks the shape later."""
    if not isinstance(value, str) or not value:
        raise _error(key, "must be a non-empty string")
    return value


def _path_list(key: str, value: object) -> object:
    """Accept a list of non-empty strings."""
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise _error(key, "must be a list of non-empty strings")
    return value


def _hosts(key: str, value: object) -> object:
    """Accept a non-empty list of unique host names."""
    items = _path_list(key, value)
    if not isinstance(items, list) or not items or len(set(items)) != len(items):
        raise _error(key, "must be a non-empty list of unique host names")
    return items


def _ports(key: str, value: object) -> object:
    """Accept a non-empty list of unique ports."""
    if (
        not isinstance(value, list)
        or not value
        or len(set(value)) != len(value)
        or not all(_is_int(port) and 1 <= port <= MAX_PORT for port in value)
    ):
        raise _error(key, f"must be a non-empty list of unique ports from 1 to {MAX_PORT}")
    return value


def _selectors(key: str, value: object) -> object:
    """Accept only the documented selectors."""
    items = _path_list(key, value)
    if not isinstance(items, list) or any(item not in SELECTORS for item in items):
        raise _error(key, f"entries must be among {sorted(SELECTORS)}")
    return items


def _boolean(key: str, value: object) -> object:
    """Accept a real boolean."""
    if not isinstance(value, bool):
        raise _error(key, "must be true or false")
    return value


def _executable(key: str, value: object) -> object:
    """Accept the word auto or an absolute path."""
    if value != "auto" and not (isinstance(value, str) and value.startswith("/")):
        raise _error(key, 'must be "auto" or an absolute path')
    return value


def _implementation(key: str, value: object) -> object:
    """Accept the name of a registered implementation, such as builtin.claude."""
    if not isinstance(value, str) or _IMPLEMENTATION.fullmatch(value) is None:
        raise _error(key, "must be an implementation name such as builtin.claude")
    return value


def _present(_key: str, value: object) -> object:
    """Accept the value as it is: the key is checked where its meaning is known."""
    return value


def _mode(key: str, value: object) -> object:
    """Accept the word read or write."""
    if value not in {mode.value for mode in TaskMode}:
        raise _error(key, 'must be "read" or "write"')
    return value


def _auto(key: str, value: object) -> object:
    """Accept only the registered invocation template."""
    if value != "auto":
        raise _error(key, 'must be "auto": no other invocation template is registered')
    return value


def _model(key: str, value: object) -> object:
    """Accept one token without whitespace that does not start with "-": one plain argument."""
    if not isinstance(value, str) or _MODEL.fullmatch(value) is None:
        raise _error(key, "must be one word without spaces that does not start with a dash")
    return value


def _bucket(key: str, value: object) -> object:
    """Accept a quota bucket name: lowercase letters, digits, underscore and hyphen."""
    if not isinstance(value, str) or _BUCKET.fullmatch(value) is None:
        raise _error(key, "must be a bucket name of lowercase letters, digits, _ and -")
    return value


def _quota_mode(key: str, value: object) -> object:
    """Accept one of the three quota modes."""
    if value not in QUOTA_MODES:
        raise _error(key, f"must be one of {', '.join(QUOTA_MODES)}")
    return value


def _mcp_transport(key: str, value: object) -> object:
    """Accept one of the two transports: `own` (shipped) or `sdk` (an optional extra)."""
    if value not in MCP_TRANSPORTS:
        raise _error(key, f"must be one of {', '.join(MCP_TRANSPORTS)}")
    return value


def _percent(key: str, value: object) -> object:
    """Accept a whole percent from 0 to 99: a reserve of 100 would refuse everything."""
    if not _is_int(value) or not isinstance(value, int) or not 0 <= value <= MAX_RESERVE:
        raise _error(key, f"must be a whole number from 0 to {MAX_RESERVE}")
    return value


def _effort(key: str, value: object) -> object:
    """Accept one of the levels `claude --help` documents."""
    if value not in EFFORT_LEVELS:
        raise _error(key, f"must be one of {', '.join(EFFORT_LEVELS)}")
    return value


_VALIDATORS: Mapping[str, Validator] = {
    "schema_version": _schema_version,
    "store.path": _path,
    "run_area.path": _path,
    "resources.run.memory_bytes": _positive_int,
    "resources.run.file_bytes": _positive_int,
    "resources.run.descriptors_per_process": _positive_int,
    "resources.run.aggregate_descriptors": _positive_int,
    "resources.capture.output_bytes": _positive_int,
    "inspector.timeout_s": _seconds,
    "inspector.memory_bytes": _positive_int,
    "mcp.transport": _mcp_transport,
    "mcp.initialize_timeout_s": _seconds,
    "mcp.partial_frame_timeout_s": _seconds,
    "mcp.write_timeout_s": _seconds,
    "mcp.shutdown_drain_s": _seconds,
    "mcp.shutdown_total_s": _seconds,
    "mcp.priority_burst": _positive_int,
    "mcp.wait_max_s": _seconds,
    "mcp.frame.in_bytes": _positive_int,
    "mcp.frame.out_bytes": _positive_int,
    "mcp.frame.depth": _positive_int,
    "mcp.frame.nodes": _positive_int,
    "mcp.frame.numeric_token_bytes": _positive_int,
    "mcp.ids.maximum": _positive_int,
    "mcp.ids.string_bytes": _positive_int,
    "mcp.normal.workers": _positive_int,
    "mcp.normal.queue": _positive_int,
    "mcp.control.workers": _positive_int,
    "mcp.control.queue": _positive_int,
    "mcp.input.aggregate_bytes": _positive_int,
    "process.observation.process_interval_s": _seconds,
    "process.observation.process_max_age_s": _seconds,
    "process.observation.path_interval_s": _seconds,
    "process.observation.path_max_age_s": _seconds,
    "process.observation.empty_rounds": _positive_int,
    "process.observation.cleanup_rounds": _positive_int,
    "watchdog.hard_s": _seconds,
    "watchdog.term_grace_s": _seconds,
    "watchdog.kill_wait_s": _seconds,
    "watchdog.scan_s": _seconds,
    "watchdog.startup_s": _seconds,
    "public.reason_bytes": _positive_int,
    "public.summary_bytes": _positive_int,
    "security.input_bytes": _positive_int,
    "logging.directory": _path,
    "proxy.ports": _ports,
    "proxy.providers.anthropic.hosts": _hosts,
    "proxy.providers.openai.hosts": _hosts,
    "sandbox.read_deny": _selectors,
    "sandbox.sensitive_user_data": _path_list,
    "sandbox.other_vendor_state": _path_list,
    "sandbox.vendor_state.anthropic": _path_list,
    "sandbox.vendor_state.openai": _path_list,
    "profiles._template.executable": _executable,
    "profiles._template.invocation_read": _auto,
    "profiles._template.invocation_write": _auto,
    "defaults.mode": _mode,
    "defaults.chain": _present,
    "defaults.recommendation_model": _model,
    "defaults.recommendation_effort": _effort,
    "defaults.quota_mode": _quota_mode,
    "defaults.quota_reserve_percent": _percent,
    "defaults.quota_max_age_s": _seconds,
    "profiles.openai.main.bound_bucket": _bucket,
    "security.allow_write_tasks": _path_list,
    "storage.sqlite_busy_s": _seconds,
    "proxy.allow_nonpublic": _boolean,
    "proxy.header_bytes": _positive_int,
    "proxy.connections": _positive_int,
    "proxy.buffer_bytes": _positive_int,
    "proxy.idle_s": _seconds,
    "proxy.lifetime_s": _seconds,
    "proxy.connect_s": _seconds,
    "providers._template.enabled": _boolean,
    "providers._template.effort": _effort,
    "providers._template.capacity": _positive_int,
}
