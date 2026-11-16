"""The resolved settings the walking skeleton consumes (ARCH section 3.2, M1 subset).

Every value is already validated and every location is an absolute path. Reading files and
resolving tokens belongs to the settings loader in the adapters layer.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from agents_dispatcher.domain.routing import Chain, TaskMode


class SettingsError(Exception):
    """A settings source cannot be used; the message says why."""


class InvalidSettingsError(SettingsError):
    """A settings source is malformed or holds an invalid value; the message names the key."""


@dataclass(frozen=True)
class TaskDefinition:
    """A task type as the settings declare it: its mode and its own chain, if it has one."""

    mode: TaskMode
    chain: Chain | None = None


@dataclass(frozen=True)
class ProviderSettings:
    """A provider as the settings declare it: the implementation that runs it and its defaults."""

    enabled: bool
    implementation: str
    model: str
    effort: str
    capacity: int


@dataclass(frozen=True)
class McpSettings:
    """The limits and clocks of the MCP server, read once when the server starts."""

    transport: str
    frame_in_bytes: int
    frame_out_bytes: int
    frame_depth: int
    frame_nodes: int
    numeric_token_bytes: int
    ids_maximum: int
    string_bytes: int
    normal_workers: int
    normal_queue: int
    control_workers: int
    control_queue: int
    initialize_timeout_s: float
    partial_frame_timeout_s: float
    write_timeout_s: float
    shutdown_drain_s: float
    shutdown_total_s: float
    priority_burst: int
    wait_max_s: float
    aggregate_bytes: int


@dataclass(frozen=True)
class Settings:
    """The M1 settings subset, resolved."""

    store_path: str
    run_area_path: str
    memory_bytes: int
    hard_s: float
    term_grace_s: float
    kill_wait_s: float
    watchdog_scan_s: float
    watchdog_startup_s: float
    public_reason_bytes: int
    public_summary_bytes: int
    logging_directory: str
    provider_hosts: Mapping[str, tuple[str, ...]]
    proxy_ports: tuple[int, ...]
    read_deny: tuple[str, ...]
    sensitive_user_data: tuple[str, ...]
    other_vendor_state: tuple[str, ...]
    vendor_state: Mapping[str, tuple[str, ...]]
    executables: Mapping[str, str]
    sqlite_busy_s: float
    inspector_timeout_s: float
    inspector_memory_bytes: int
    mcp: McpSettings
    proxy_allow_nonpublic: bool
    proxy_header_bytes: int
    proxy_connections: int
    proxy_buffer_bytes: int
    proxy_idle_s: float
    proxy_lifetime_s: float
    proxy_connect_s: float
    input_bytes: int
    default_mode: TaskMode
    recommendation_model: str
    recommendation_effort: str
    quota_mode: str
    quota_reserve_percent: int
    quota_max_age_s: float
    bound_buckets: Mapping[str, str]
    tasks: Mapping[str, TaskDefinition]
    allow_write_tasks: tuple[str, ...]
    providers: Mapping[str, ProviderSettings]
    default_chain: Chain
