"""Spies for the ports of the run use case; every call lands in one shared journal."""
# ruff: noqa: ARG002 - spies must match the port signatures even where they ignore an argument

from collections.abc import Callable
from dataclasses import dataclass, field

from agents_dispatcher.application.errors import LaunchError, WorkspaceNotGitError
from agents_dispatcher.application.ports import (
    EgressEndpoint,
    ExecutionOutcome,
    FinishRecord,
    PreparedWorkspace,
    ProcessIdentity,
    SealedResult,
    WorkerCommand,
    WorkerOutcome,
)
from agents_dispatcher.domain.run import Limits, RunId, RunState
from tests.support.clock import FakeClock

RUN_ID = RunId("3f2b8c1e-5d4a-4b7e-9c3a-1f2e3d4c5b6a")
BASE = "b" * 40


@dataclass
class Journal:
    """The ordered record of every port call."""

    events: list[str] = field(default_factory=list)

    def add(self, event: str) -> None:
        """Append one event."""
        self.events.append(event)


class SpyIds:
    """Hands out the same run id every time."""

    def __init__(self, journal: Journal, value: RunId = RUN_ID) -> None:
        """Remember the journal and the id to return."""
        self.journal = journal
        self.value = value

    def new_run_id(self) -> RunId:
        """Return the configured id."""
        self.journal.add("ids.new")
        return self.value


class SpyWorkspace:
    """A workspace that prepares, seals and retires nothing real."""

    def __init__(self, journal: Journal) -> None:
        """Remember the journal and default behaviour."""
        self.journal = journal
        self.not_git = False
        self.prepare_error: Exception | None = None
        self.sealed = SealedResult(branch=RUN_ID.branch_name, commits_on_branch=1)
        self.notes: tuple[str, ...] = ("uncommitted changes not included",)
        self.keep_sandbox_calls: list[bool] = []

    def prepare(self, repo: str, run_id: RunId) -> PreparedWorkspace:
        """Return a prepared workspace or refuse a non-Git directory."""
        self.journal.add("workspace.prepare")
        if self.prepare_error is not None:
            raise self.prepare_error
        if self.not_git:
            message = f"{repo} is not a Git repository"
            raise WorkspaceNotGitError(message)
        return PreparedWorkspace(
            base_commit=BASE,
            run_id=run_id,
            sandbox_dir=f"/runs/{run_id.sandbox_name}",
            worktree_dir=f"/runs/{run_id.sandbox_name}/worktree",
            branch_name=run_id.branch_name,
            notes=self.notes,
        )

    def seal(self, prepared: PreparedWorkspace) -> SealedResult:
        """Return the configured result."""
        self.journal.add("workspace.seal")
        return self.sealed

    def retire(self, prepared: PreparedWorkspace, *, keep_sandbox: bool) -> None:
        """Record the retirement and whether the sandbox stays."""
        self.journal.add("workspace.retire")
        self.keep_sandbox_calls.append(keep_sandbox)


class SpyEgress:
    """A proxy that never listens."""

    def __init__(self, journal: Journal) -> None:
        """Remember the journal."""
        self.journal = journal
        self.canned_faults: tuple[str, ...] = ()

    def faults(self) -> tuple[str, ...]:
        """Return the canned listener faults."""
        return self.canned_faults

    def start(self, run_id: RunId) -> EgressEndpoint:
        """Return a fixed loopback endpoint."""
        self.journal.add("egress.start")
        return EgressEndpoint(host="127.0.0.1", port=40123)

    def stop(self) -> None:
        """Record the stop."""
        self.journal.add("egress.stop")


class SpyWorker:
    """A worker adapter with a canned command and a canned verdict."""

    def __init__(self, journal: Journal) -> None:
        """Remember the journal and default behaviour."""
        self.journal = journal
        self.outcome = WorkerOutcome(succeeded=True, result_text="done", model="claude-sonnet-5-5")

    def cli_version(self) -> str:
        """Return a fixed version."""
        self.journal.add("worker.version")
        return "2.1.304"

    def command(
        self, task: str, workspace: PreparedWorkspace, egress: EgressEndpoint
    ) -> WorkerCommand:
        """Return a command that carries the task on standard input."""
        self.journal.add("worker.command")
        return WorkerCommand(
            argv=("fake-claude", "-p"),
            environment={"HTTPS_PROXY": f"http://{egress.host}:{egress.port}"},
            stdin=task,
            cwd=workspace.worktree_dir,
        )

    def parse(self, outcome: ExecutionOutcome) -> WorkerOutcome:
        """Return the configured verdict."""
        self.journal.add("worker.parse")
        return self.outcome


class SpyConfinement:
    """A confinement that returns a canned outcome and advances the fake clock."""

    def __init__(self, journal: Journal, clock: FakeClock) -> None:
        """Remember the journal, the clock and default behaviour."""
        self.journal = journal
        self.clock = clock
        self.duration_s = 7.0
        self.outcome = ExecutionOutcome(exit_status=0, stdout="{}")
        self.launch_error: LaunchError | None = None
        self.limits_seen: list[Limits] = []
        self.egress_seen: list[EgressEndpoint] = []
        self.stop_polled_true_after: int | None = None

    def run(  # noqa: PLR0913, PLR0917 - mirrors the port signature
        self,
        command: WorkerCommand,
        workspace: PreparedWorkspace,
        egress: EgressEndpoint,
        limits: Limits,
        on_started: Callable[[ProcessIdentity], None],
        should_stop: Callable[[], bool],
    ) -> ExecutionOutcome:
        """Report the process identity, advance time and return the outcome."""
        self.journal.add("confinement.run")
        if self.stop_polled_true_after is not None:
            for _ in range(self.stop_polled_true_after):
                should_stop()
            if should_stop():
                should_stop()  # the watch loop may ask again before the group is gone
                self.journal.add("confinement.stop_seen")
                return ExecutionOutcome(exit_status=None, stdout="", stopped=True)
        self.limits_seen.append(limits)
        self.egress_seen.append(egress)
        if self.launch_error is not None:
            raise self.launch_error
        on_started(ProcessIdentity(pgid=4242, start_time_s=1.5))
        self.clock.advance(self.duration_s)
        return self.outcome


class SpyStops:
    """A stop poll whose answer the test sets; every question lands in the journal."""

    def __init__(self, journal: Journal) -> None:
        """Remember the journal; no stop is requested at first."""
        self.journal = journal
        self.requested_now = False
        self.requested_from_call: int | None = None
        self.reason: str | None = None
        self.calls = 0

    def stop_requested(self, run_id: RunId) -> bool:
        """Answer from the configuration."""
        self.calls += 1
        self.journal.add("stops.ask")
        if self.requested_from_call is not None and self.calls >= self.requested_from_call:
            return True
        return self.requested_now

    def stop_reason(self, run_id: RunId) -> str | None:
        """Answer the configured reason."""
        return self.reason


class SpyOwnership:
    """The held lock of one run."""

    def __init__(self, journal: Journal) -> None:
        """Remember the journal."""
        self.journal = journal

    def release(self) -> None:
        """Record the release."""
        self.journal.add("owner.release")


class SpyOwners:
    """An owner registry that records when the run is held."""

    def __init__(self, journal: Journal) -> None:
        """Remember the journal."""
        self.journal = journal

    def hold(self, run_id: RunId) -> SpyOwnership:
        """Record the hold."""
        self.journal.add("owner.hold")
        return SpyOwnership(self.journal)


class SpyStore:
    """A run store that keeps what it was told."""

    def __init__(self, journal: Journal) -> None:
        """Remember the journal."""
        self.journal = journal
        self.states: list[RunState] = []
        self.identities: list[ProcessIdentity] = []
        self.finished: list[FinishRecord] = []
        self.claimed: list[ProcessIdentity] = []
        self.claim_answer = True
        self.workspace_facts: list[tuple[str, str]] = []

    def claim(self, run_id: RunId, supervisor: ProcessIdentity) -> bool:
        """Record the claim; the answer is configurable."""
        self.journal.add("store.claim")
        self.claimed.append(supervisor)
        return self.claim_answer

    def record_workspace(self, run_id: RunId, *, base_commit: str, cli_version: str) -> None:
        """Record the facts the supervisor learned."""
        self.journal.add("store.workspace")
        self.workspace_facts.append((base_commit, cli_version))

    def set_state(self, run_id: RunId, state: RunState) -> None:
        """Record a state change."""
        self.journal.add(f"store.state {state.name}")
        self.states.append(state)

    def set_process(self, run_id: RunId, identity: ProcessIdentity) -> None:
        """Record the worker group identity."""
        self.journal.add("store.process")
        self.identities.append(identity)

    def finish(self, run_id: RunId, record: FinishRecord) -> None:
        """Record the terminal outcome."""
        self.journal.add(f"store.finish {record.terminal_reason.name}")
        self.finished.append(record)
