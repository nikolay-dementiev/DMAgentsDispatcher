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

from agents_dispatcher.application.settings import Settings, SettingsError

SCHEMA_VERSION = 1
SELECTORS = frozenset(
    {"@sensitive-user-data", "@other-vendor-state", "@sdk-control", "@other-runs"}
)
MAX_PORT = 65535
_SECONDS = re.compile(r"\d+(\.\d{1,9})?")
_TOKEN = re.compile(r"(@[a-z-]+)(/.*)?")

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


def user_settings_path(account_home: str) -> str:
    """Return where the optional user settings file lives (ARCH section 3.1)."""
    return f"{account_home}/.config/agents-dispatcher/settings.toml"


def load_settings(*, defaults_text: str, user_text: str | None, account_home: str) -> Settings:
    """Merge the user file over the defaults, validate every key and resolve the locations."""
    values = _flatten(_parse(defaults_text, "defaults"))
    if user_text is not None:
        for key, value in _flatten(_parse(user_text, "user file")).items():
            if key not in values:
                raise _error(key, "unknown key")
            values[key] = value
    missing = sorted(set(_VALIDATORS) - set(values))
    if missing:
        raise _error(missing[0], "missing from the defaults")
    checked = {key: check(key, values[key]) for key, check in _VALIDATORS.items()}
    store = _resolve(account_home, None, "store.path", str(checked["store.path"]))
    return Settings(
        store_path=store,
        run_area_path=_resolve(account_home, store, "run_area.path", str(checked["run_area.path"])),
        memory_bytes=int(str(checked["resources.run.memory_bytes"])),
        hard_s=float(str(checked["watchdog.hard_s"])),
        term_grace_s=float(str(checked["watchdog.term_grace_s"])),
        kill_wait_s=float(str(checked["watchdog.kill_wait_s"])),
        anthropic_hosts=tuple(_strings(checked["proxy.providers.anthropic.hosts"])),
        proxy_ports=tuple(_ints(checked["proxy.ports"])),
        read_deny=tuple(_strings(checked["sandbox.read_deny"])),
        sensitive_user_data=_resolve_all(
            account_home, store, "sandbox.sensitive_user_data", checked
        ),
        other_vendor_state=_resolve_all(account_home, store, "sandbox.other_vendor_state", checked),
        worker_executable=str(checked["profiles._template.executable"]),
        sqlite_busy_s=float(str(checked["storage.sqlite_busy_s"])),
        proxy_allow_nonpublic=checked["proxy.allow_nonpublic"] is True,
        proxy_header_bytes=int(str(checked["proxy.header_bytes"])),
        proxy_connections=int(str(checked["proxy.connections"])),
        proxy_buffer_bytes=int(str(checked["proxy.buffer_bytes"])),
        proxy_idle_s=float(str(checked["proxy.idle_s"])),
        proxy_lifetime_s=float(str(checked["proxy.lifetime_s"])),
    )


def _error(key: str, problem: str) -> SettingsError:
    """Build the error that names the key."""
    return SettingsError(f"settings: {key}: {problem}")


def _parse(text: str, source: str) -> dict[str, Any]:
    """Parse TOML text; a syntax error becomes a settings error."""
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as error:
        message = f"settings: cannot parse the {source}: {error}"
        raise SettingsError(message) from error


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


_VALIDATORS: Mapping[str, Validator] = {
    "schema_version": _schema_version,
    "store.path": _path,
    "run_area.path": _path,
    "resources.run.memory_bytes": _positive_int,
    "watchdog.hard_s": _seconds,
    "watchdog.term_grace_s": _seconds,
    "watchdog.kill_wait_s": _seconds,
    "proxy.ports": _ports,
    "proxy.providers.anthropic.hosts": _hosts,
    "sandbox.read_deny": _selectors,
    "sandbox.sensitive_user_data": _path_list,
    "sandbox.other_vendor_state": _path_list,
    "profiles._template.executable": _executable,
    "storage.sqlite_busy_s": _seconds,
    "proxy.allow_nonpublic": _boolean,
    "proxy.header_bytes": _positive_int,
    "proxy.connections": _positive_int,
    "proxy.buffer_bytes": _positive_int,
    "proxy.idle_s": _seconds,
    "proxy.lifetime_s": _seconds,
}
