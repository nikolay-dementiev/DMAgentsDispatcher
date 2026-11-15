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
    Settings,
    SettingsError,
    TaskDefinition,
)
from agents_dispatcher.domain.routing import TaskMode, validate_name

SCHEMA_VERSION = 1
SELECTORS = frozenset(
    {"@sensitive-user-data", "@other-vendor-state", "@sdk-control", "@other-runs"}
)
MAX_PORT = 65535
_SECONDS = re.compile(r"\d+(\.\d{1,9})?")
_MODEL = re.compile(r"[^\s-]\S*")
EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
_TOKEN = re.compile(r"(@[a-z-]+)(/.*)?")
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
    if user_text is not None:
        for key, value in _flatten(_parse(user_text, "user file")).items():
            if key not in values and _TASK_KEY.fullmatch(key) is None:
                raise _error(key, "unknown key")
            values[key] = value
    missing = sorted(set(_VALIDATORS) - set(values))
    if missing:
        raise _error(missing[0], "missing from the defaults")
    checked = {key: check(key, values[key]) for key, check in _VALIDATORS.items()}
    store = _resolve(account_home, None, "store.path", str(checked["store.path"]))
    tasks = _tasks(values)
    return Settings(
        store_path=store,
        run_area_path=_resolve(account_home, store, "run_area.path", str(checked["run_area.path"])),
        memory_bytes=int(str(checked["resources.run.memory_bytes"])),
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
        anthropic_hosts=tuple(_strings(checked["proxy.providers.anthropic.hosts"])),
        proxy_ports=tuple(_ints(checked["proxy.ports"])),
        read_deny=tuple(_strings(checked["sandbox.read_deny"])),
        sensitive_user_data=_resolve_all(
            account_home, store, "sandbox.sensitive_user_data", checked
        ),
        other_vendor_state=_resolve_all(account_home, store, "sandbox.other_vendor_state", checked),
        input_bytes=int(str(checked["security.input_bytes"])),
        worker_executable=str(checked["profiles._template.executable"]),
        sqlite_busy_s=float(str(checked["storage.sqlite_busy_s"])),
        proxy_allow_nonpublic=checked["proxy.allow_nonpublic"] is True,
        proxy_header_bytes=int(str(checked["proxy.header_bytes"])),
        proxy_connections=int(str(checked["proxy.connections"])),
        proxy_buffer_bytes=int(str(checked["proxy.buffer_bytes"])),
        proxy_idle_s=float(str(checked["proxy.idle_s"])),
        proxy_lifetime_s=float(str(checked["proxy.lifetime_s"])),
        proxy_connect_s=float(str(checked["proxy.connect_s"])),
        worker_model=str(checked["providers.anthropic.model"]),
        worker_effort=str(checked["providers._template.effort"]),
        default_mode=TaskMode(str(checked["defaults.mode"])),
        recommendation_model=str(checked["defaults.recommendation_model"]),
        recommendation_effort=str(checked["defaults.recommendation_effort"]),
        tasks=tasks,
        allow_write_tasks=_allowed_writes(checked["security.allow_write_tasks"], tasks),
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


def _tasks(values: Mapping[str, object]) -> dict[str, TaskDefinition]:
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
        tasks[name] = TaskDefinition(mode=TaskMode(str(_mode(mode_key, values[mode_key]))))
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
    "sandbox.read_deny": _selectors,
    "sandbox.sensitive_user_data": _path_list,
    "sandbox.other_vendor_state": _path_list,
    "profiles._template.executable": _executable,
    "profiles._template.invocation_read": _auto,
    "profiles._template.invocation_write": _auto,
    "defaults.mode": _mode,
    "defaults.recommendation_model": _model,
    "defaults.recommendation_effort": _effort,
    "security.allow_write_tasks": _path_list,
    "storage.sqlite_busy_s": _seconds,
    "proxy.allow_nonpublic": _boolean,
    "proxy.header_bytes": _positive_int,
    "proxy.connections": _positive_int,
    "proxy.buffer_bytes": _positive_int,
    "proxy.idle_s": _seconds,
    "proxy.lifetime_s": _seconds,
    "proxy.connect_s": _seconds,
    "providers.anthropic.model": _model,
    "providers._template.effort": _effort,
}
