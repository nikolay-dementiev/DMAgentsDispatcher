"""The run: its identifier, states, terminal reasons, limits, task and report.

Names follow the architecture draft 4: run states at L746, terminal reasons at L808
and L1432. This module is pure; it performs no input or output.
"""

import re
from dataclasses import dataclass
from enum import Enum

from agents_dispatcher.domain.routing import RunRouting, validate_name

RESULT_TEXT_LIMIT = 20_000
RUN_ID_PATTERN = "[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}"
_UUID4 = re.compile(RUN_ID_PATTERN)
# The run store keeps a memory limit as a signed 64-bit integer.
MAX_MEMORY_BYTES = 2**63 - 1
# Members of the report object that a reader of the stored text (the command line) looks up.
LIMITS_MEMBER = "limits"
LIMIT_HIT_MEMBER = "hit"
RESULT_TEXT_MEMBER = "result_text"


@dataclass(frozen=True)
class RunId:
    """A canonical lowercase UUIDv4; it names the run sandbox and the result branch."""

    value: str

    def __post_init__(self) -> None:
        """Refuse anything that is not a canonical UUIDv4, path tricks included."""
        if not _UUID4.fullmatch(self.value):
            message = f"run id must be a canonical lowercase UUIDv4, got {self.value!r}"
            raise ValueError(message)

    @property
    def sandbox_name(self) -> str:
        """Return the run sandbox directory name."""
        return f"agentsd-run-{self.value}"

    @property
    def branch_name(self) -> str:
        """Return the result branch name."""
        return f"agentsd-run-{self.value}"


class RunState(Enum):
    """The run states the walking skeleton reaches (ARCH L746)."""

    ACCEPTED = "ACCEPTED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPING = "STOPPING"
    FINALIZING = "FINALIZING"
    QUARANTINED = "QUARANTINED"
    TERMINAL = "TERMINAL"


class TerminalReason(Enum):
    """Why a run ended (ARCH L808, L1432)."""

    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    RESOURCE_LIMIT = "RESOURCE_LIMIT"
    STOPPED = "STOPPED"
    ORPHANED = "ORPHANED"
    LAUNCH_LOST = "LAUNCH_LOST"


@dataclass(frozen=True)
class Survivor:
    """A process that belongs to a run and could not be ended, named by pid and start time.

    `sources` says why it is attributed to the run (group, descendant, marker, folder).
    """

    pid: int
    start_time: float
    sources: tuple[str, ...]


class LimitKind(Enum):
    """A limit, named by the settings key it comes from."""

    TIME = "watchdog.hard_s"
    MEMORY = "resources.run.memory_bytes"
    OUTPUT = "resources.capture.output_bytes"
    DESCRIPTORS = "resources.run.aggregate_descriptors"
    SANDBOX_BYTES = "resources.run.sandbox_bytes"
    SANDBOX_INODES = "resources.run.sandbox_inodes"


@dataclass(frozen=True)
class Limits:
    """The resource limits of one run."""

    hard_s: float
    memory_bytes: int

    def __post_init__(self) -> None:
        """Refuse a limit that would stop every run at once."""
        if self.hard_s <= 0:
            message = f"hard_s must be positive, got {self.hard_s}"
            raise ValueError(message)
        if self.memory_bytes <= 0:
            message = f"memory_bytes must be positive, got {self.memory_bytes}"
            raise ValueError(message)


@dataclass(frozen=True)
class TaskSpec:
    """What the caller asked for: a repository, a task and optional per-run overrides."""

    repo: str
    task: str
    hard_s: float | None = None
    memory_bytes: int | None = None
    task_type: str | None = None
    provider: str | None = None

    def __post_init__(self) -> None:
        """Refuse blank text, bad names and non-positive overrides before anything starts."""
        if self.task_type is not None:
            validate_name(self.task_type, "task type")
        if self.provider is not None:
            validate_name(self.provider, "provider id")
        if not self.repo.strip():
            message = "repo must not be blank"
            raise ValueError(message)
        if not self.task.strip():
            message = "task must not be blank"
            raise ValueError(message)
        try:
            self.task.encode("utf-8")
        except UnicodeEncodeError as error:
            message = f"task is not valid UTF-8 text (position {error.start}); use valid UTF-8"
            raise ValueError(message) from error
        if self.hard_s is not None and self.hard_s <= 0:
            message = f"hard_s override must be positive, got {self.hard_s}"
            raise ValueError(message)
        if self.memory_bytes is not None and self.memory_bytes <= 0:
            message = f"memory_bytes override must be positive, got {self.memory_bytes}"
            raise ValueError(message)
        if self.memory_bytes is not None and self.memory_bytes > MAX_MEMORY_BYTES:
            message = (
                f"memory_bytes override must be at most {MAX_MEMORY_BYTES}, got {self.memory_bytes}"
            )
            raise ValueError(message)


@dataclass(frozen=True)
class RunReport:
    """The result of one run; `as_dict` is the JSON contract of `agentsd run`."""

    run_id: RunId
    state: RunState
    terminal_reason: TerminalReason | None
    repo: str
    base_commit: str | None
    branch: str | None
    commits_on_branch: int
    worker_cli_version: str | None
    worker_model: str | None
    worker_model_source: str | None
    worker_exit_status: int | None
    duration_s: float
    limits_configured: Limits | None
    limit_hit: LimitKind | None
    survivors: tuple[str, ...]
    result_text: str
    notes: tuple[str, ...]
    routing: RunRouting
    problem: str | None = None

    def as_dict(self) -> dict[str, object]:
        """Return the report as plain values in contract order."""
        text = self.result_text
        notes = list(self.notes)
        if len(text) > RESULT_TEXT_LIMIT:
            text = text[:RESULT_TEXT_LIMIT]
            notes.append("result_text truncated")
        return {
            "run_id": self.run_id.value,
            "state": self.state.name,
            "terminal_reason": self.terminal_reason.name if self.terminal_reason else None,
            "problem": self.problem,
            "task_type": self.routing.task_type,
            "mode": self.routing.mode.value,
            "provider": self.routing.provider,
            "repo": self.repo,
            "base_commit": self.base_commit,
            "branch": self.branch,
            "commits_on_branch": self.commits_on_branch,
            "worker": {
                "cli_version": self.worker_cli_version,
                "exit_status": self.worker_exit_status,
                "model": self.worker_model,
                "model_source": self.worker_model_source,
            },
            "route": [item.as_dict() for item in self.routing.route],
            "duration_s": self.duration_s,
            LIMITS_MEMBER: {
                "configured": {
                    "hard_s": self.limits_configured.hard_s,
                    "memory_bytes": self.limits_configured.memory_bytes,
                }
                if self.limits_configured
                else None,
                LIMIT_HIT_MEMBER: self.limit_hit.value if self.limit_hit else None,
            },
            "survivors": list(self.survivors),
            RESULT_TEXT_MEMBER: text,
            "notes": notes,
        }
