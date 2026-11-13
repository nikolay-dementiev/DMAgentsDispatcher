"""The ports of the run use case and the plain values that cross them.

Port names follow the architecture draft 4 section 2.7 (ARCH L820-890), narrowed to what
the walking skeleton calls. Paths are plain strings because the inner layers do no I/O.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from agents_dispatcher.domain.run import LimitKind, Limits, RunId, RunState, TerminalReason


@dataclass(frozen=True)
class PreparedWorkspace:
    """A run sandbox with a worktree on its own branch, ready for the worker."""

    base_commit: str
    run_id: RunId
    sandbox_dir: str
    worktree_dir: str
    branch_name: str
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class SealedResult:
    """What the worker left: a result branch, or nothing when it changed no file."""

    branch: str | None
    commits_on_branch: int


@dataclass(frozen=True)
class EgressEndpoint:
    """The loopback address of the run's allowlist proxy."""

    host: str
    port: int


@dataclass(frozen=True)
class WorkerCommand:
    """An argument array, an environment, the brief for standard input and a working directory."""

    argv: tuple[str, ...]
    environment: Mapping[str, str]
    stdin: str
    cwd: str


@dataclass(frozen=True)
class ProcessIdentity:
    """A process group and the start time of its leader; together they survive pid reuse.

    `start_time_s` is None when the leader's start time could not be read; a row without it is
    never trusted to identify a live group.
    """

    pgid: int
    start_time_s: float | None


@dataclass(frozen=True)
class ExecutionOutcome:
    """What the confined execution reports after the worker group is gone or given up on."""

    exit_status: int | None
    stdout: str
    survivors: tuple[str, ...] = ()
    survivors_alive: tuple[str, ...] = ()
    limit_hit: LimitKind | None = None
    stopped: bool = False


@dataclass(frozen=True)
class WorkerOutcome:
    """The worker adapter's reading of the execution outcome."""

    succeeded: bool
    result_text: str


@dataclass(frozen=True)
class RunRecord:
    """The run as first stored."""

    run_id: RunId
    repo: str
    base_commit: str
    branch: str
    cli_version: str


@dataclass(frozen=True)
class FinishRecord:
    """The terminal outcome as stored."""

    terminal_reason: TerminalReason
    branch: str | None
    commits_on_branch: int
    limit_hit: LimitKind | None
    worker_exit_status: int | None
    duration_s: float


class IdGenerator(Protocol):
    """Source of fresh run identifiers."""

    def new_run_id(self) -> RunId:
        """Return an identifier never used before."""
        ...


class Clock(Protocol):
    """A monotonic clock that keeps counting while the machine sleeps (ARCH section 4.7)."""

    def now(self) -> float:
        """Return seconds on a suspend-inclusive monotonic scale."""
        ...


class Workspace(Protocol):
    """The Git side of a run (ARCH `GitBroker`, narrowed)."""

    def prepare(self, repo: str, run_id: RunId) -> PreparedWorkspace:
        """Create sandbox and worktree, or raise `WorkspaceNotGitError` having created nothing."""
        ...

    def seal(self, prepared: PreparedWorkspace) -> SealedResult:
        """Publish what the worker left as the result branch."""
        ...

    def retire(self, prepared: PreparedWorkspace, *, keep_sandbox: bool) -> None:
        """Remove the worktree and prune it; keep the sandbox when asked."""
        ...


class EgressProxy(Protocol):
    """The CONNECT allowlist proxy of one run (ARCH `ProxyManager`, narrowed)."""

    def start(self, run_id: RunId) -> EgressEndpoint:
        """Listen on loopback and return the address."""
        ...

    def stop(self) -> None:
        """Stop listening."""
        ...


class Worker(Protocol):
    """One agent CLI behind the worker adapter interface."""

    def cli_version(self) -> str:
        """Return the installed CLI version."""
        ...

    def command(
        self, task: str, workspace: PreparedWorkspace, egress: EgressEndpoint
    ) -> WorkerCommand:
        """Build the command; the brief goes on standard input."""
        ...

    def parse(self, outcome: ExecutionOutcome) -> WorkerOutcome:
        """Read the execution outcome."""
        ...


class Confinement(Protocol):
    """Runs a command inside the confinement and cleans up after it (ARCH `Execution`)."""

    def run(
        self,
        command: WorkerCommand,
        workspace: PreparedWorkspace,
        egress: EgressEndpoint,
        limits: Limits,
        on_started: Callable[[ProcessIdentity], None],
    ) -> ExecutionOutcome:
        """Run, watch, stop and sweep; raise `LaunchError` when the worker never started."""
        ...


class RunStore(Protocol):
    """The durable run record."""

    def begin(self, record: RunRecord) -> None:
        """Store a new run in state ACCEPTED."""
        ...

    def set_state(self, run_id: RunId, state: RunState) -> None:
        """Store a state change."""
        ...

    def set_process(self, run_id: RunId, identity: ProcessIdentity) -> None:
        """Store the worker group identity."""
        ...

    def finish(self, run_id: RunId, record: FinishRecord) -> None:
        """Store the terminal outcome and the state TERMINAL."""
        ...
