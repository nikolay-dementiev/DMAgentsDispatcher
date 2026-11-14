"""The ports of the run use case and the plain values that cross them.

Port names follow the architecture draft 4 section 2.7 (ARCH L820-890), narrowed to what
the walking skeleton calls. Paths are plain strings because the inner layers do no I/O.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from agents_dispatcher.domain.run import (
    LimitKind,
    Limits,
    RunId,
    RunReport,
    RunState,
    TerminalReason,
)
from agents_dispatcher.domain.views import StopDisposition, StopInfo


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
    model: str | None = None


@dataclass(frozen=True)
class AcceptedRun:
    """What `start` records before any supervisor or worktree exists."""

    run_id: RunId
    repo: str
    task: str
    limits: Limits


@dataclass(frozen=True)
class FinishRecord:
    """The terminal outcome as stored."""

    terminal_reason: TerminalReason
    branch: str | None
    commits_on_branch: int
    limit_hit: LimitKind | None
    worker_exit_status: int | None
    duration_s: float
    report_json: str


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

    def faults(self) -> tuple[str, ...]:
        """Return the listener faults of this run, one text per distinct failure streak."""
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

    def run(  # noqa: PLR0913, PLR0917 - the port carries exactly these inputs
        self,
        command: WorkerCommand,
        workspace: PreparedWorkspace,
        egress: EgressEndpoint,
        limits: Limits,
        on_started: Callable[[ProcessIdentity], None],
        should_stop: Callable[[], bool],
    ) -> ExecutionOutcome:
        """Run, watch, stop and sweep; `should_stop` is asked at every scan of the watch loop.

        Raise `LaunchError` when the worker never started.
        """
        ...


class ReportRenderer(Protocol):
    """Turns a report into the text the store keeps and the commands print."""

    def render(self, report: RunReport) -> str:
        """Return the report text."""
        ...


class RunStore(Protocol):
    """The durable run record."""

    def claim(self, run_id: RunId, supervisor: ProcessIdentity) -> bool:
        """Move ACCEPTED to STARTING for this supervisor; False when the run was not ACCEPTED."""
        ...

    def record_workspace(self, run_id: RunId, *, base_commit: str, cli_version: str) -> None:
        """Store the facts the supervisor learns once the worktree exists."""
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


@dataclass(frozen=True)
class RunSnapshot:
    """A run as the read side sees it (the stored row, without process details)."""

    run_id: RunId
    task: str
    state: RunState
    terminal_reason: TerminalReason | None
    branch: str | None
    revision: int
    stop: StopInfo | None
    report_json: str | None
    fault: str | None


class RepositoryCheck(Protocol):
    """Checks a repository before a run is created."""

    def require_git(self, repo: str) -> None:
        """Raise `WorkspaceNotGitError` unless `repo` is a Git working tree; create nothing."""
        ...


class SupervisorLauncher(Protocol):
    """Starts the detached supervisor of one run and hands it the brief."""

    def launch(self, run_id: RunId, brief: str) -> None:
        """Start the supervisor; raise `LaunchError` when it cannot be started."""
        ...


class StartStore(Protocol):
    """The store operations `start` uses."""

    def accept(self, run: AcceptedRun) -> None:
        """Store a new run in state ACCEPTED."""
        ...

    def find(self, run_id: RunId) -> RunSnapshot | None:
        """Return the run, or None."""
        ...

    def finish_unclaimed(self, run_id: RunId, record: FinishRecord) -> bool:
        """Close a run nobody took over; False when it is no longer ACCEPTED."""
        ...


class RunReader(Protocol):
    """Read access to stored runs."""

    def find(self, run_id: RunId) -> RunSnapshot | None:
        """Return the run, or None when no run has this identifier."""
        ...


class StopPoll(Protocol):
    """Tells the supervisor whether someone asked for the run to stop."""

    def stop_requested(self, run_id: RunId) -> bool:
        """Return whether a stop request is recorded for the run."""
        ...

    def stop_reason(self, run_id: RunId) -> str | None:
        """Return the reason given with the stop request, or None when there is none."""
        ...


class StopRegistry(Protocol):
    """Records stop requests atomically with the check of the run state."""

    def request_stop(self, run_id: RunId, reason: str) -> StopDisposition | None:
        """Record the request when it is effective; return what it met, None for an unknown run."""
        ...


class Ownership(Protocol):
    """The held ownership of one run."""

    def release(self) -> None:
        """Give the run up; ending the process does the same."""
        ...


class OwnerRegistry(Protocol):
    """Lets exactly one supervisor own a run at a time."""

    def hold(self, run_id: RunId) -> Ownership:
        """Take the ownership, or raise when the run already has a live owner."""
        ...


class OwnerProbe(Protocol):
    """Tells whether a run still has a live supervisor."""

    def owner_alive(self, run_id: RunId) -> bool:
        """Return whether some process holds the run's ownership."""
        ...


class RunSweeper(Protocol):
    """Closes every run that was abandoned."""

    def sweep(self) -> list[RunId]:
        """Stop and close each unfinished run that has no live owner; return their ids."""
        ...


class OrphanCloser(Protocol):
    """Closes a run whose supervisor is gone."""

    def close_if_orphaned(self, run_id: RunId) -> bool:
        """Close the run as ORPHANED when it is unfinished and has no live owner."""
        ...


class Sleeper(Protocol):
    """Waits for real time to pass."""

    def sleep(self, seconds: float) -> None:
        """Block for about `seconds`."""
        ...
