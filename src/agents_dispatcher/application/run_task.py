"""The RunTask use case: one coding task from a repository to a result branch."""

from collections.abc import Callable
from dataclasses import dataclass

from agents_dispatcher.application.errors import (
    LaunchError,
    RunNotClaimableError,
    WorkspaceError,
    WorkspaceNotGitError,
)
from agents_dispatcher.application.ports import (
    Clock,
    Confinement,
    EgressProxy,
    ExecutionOutcome,
    FinishRecord,
    OwnerRegistry,
    PreparedWorkspace,
    ProcessIdentity,
    ReportRenderer,
    RunStore,
    SealedResult,
    StopPoll,
    Worker,
    WorkerInvocation,
    Workspace,
)
from agents_dispatcher.domain.routing import RunRouting, TaskMode
from agents_dispatcher.domain.run import (
    Limits,
    RunId,
    RunReport,
    RunState,
    TaskSpec,
    TerminalReason,
)


@dataclass(frozen=True)
class _Job:
    """Everything `_drive` needs about one prepared run."""

    spec: TaskSpec
    run_id: RunId
    prepared: PreparedWorkspace
    limits: Limits
    cli_version: str
    routing: RunRouting


@dataclass
class _Cleanup:
    """What the entry point needs to know to clean up, set while the run proceeds."""

    keep_sandbox: bool = False


def _invocation(routing: RunRouting) -> WorkerInvocation:
    """Return what the stored routing tells the worker; a row without a model is not guessed at."""
    if routing.model is None or routing.effort is None:
        message = "the run has no model and effort recorded"
        raise LaunchError(message)
    return WorkerInvocation(model=routing.model, effort=routing.effort, mode=routing.mode)


class RunTask:
    """Runs one task through the ports; every collaborator arrives through the constructor."""

    def __init__(  # noqa: PLR0913 - the composition root wires exactly these collaborators
        self,
        *,
        workspace: Workspace,
        egress: EgressProxy,
        worker: Worker,
        confinement: Confinement,
        store: RunStore,
        clock: Clock,
        owners: OwnerRegistry,
        stops: StopPoll,
        renderer: ReportRenderer,
        default_limits: Limits,
    ) -> None:
        """Keep the ports and the configured limits."""
        self._workspace = workspace
        self._egress = egress
        self._worker = worker
        self._confinement = confinement
        self._store = store
        self._clock = clock
        self._owners = owners
        self._stops = stops
        self._renderer = renderer
        self._defaults = default_limits

    def supervise(
        self, spec: TaskSpec, run_id: RunId, supervisor: ProcessIdentity, routing: RunRouting
    ) -> RunReport:
        """Adopt the run that `start` accepted, run it to its end and return the report.

        The claim comes first: it is the acknowledgement `start` waits for. A worktree that
        cannot be made ends the run as FAILED with a report instead of leaving it in STARTING.
        `routing` is the decision stored at accept; it is obeyed, never resolved again.
        """
        ownership = self._owners.hold(run_id)
        try:
            if not self._store.claim(run_id, supervisor):
                message = f"run {run_id.value} is no longer waiting for a supervisor"
                raise RunNotClaimableError(message)
            limits = self._limits(spec)
            try:
                prepared = self._workspace.prepare(spec.repo, run_id)
            except (WorkspaceError, WorkspaceNotGitError) as error:
                return self._fail_before_worktree(spec, run_id, limits, routing, error)
            cleanup = _Cleanup()
            try:
                cli_version = self._worker.cli_version()
                self._store.record_workspace(
                    run_id, base_commit=prepared.base_commit, cli_version=cli_version
                )
                return self._drive(
                    _Job(spec, run_id, prepared, limits, cli_version, routing), cleanup
                )
            finally:
                self._workspace.retire(prepared, keep_sandbox=cleanup.keep_sandbox)
        finally:
            ownership.release()

    def _limits(self, spec: TaskSpec) -> Limits:
        """Resolve the limits of a run: the spec's, else the configured defaults."""
        return Limits(
            hard_s=spec.hard_s or self._defaults.hard_s,
            memory_bytes=spec.memory_bytes or self._defaults.memory_bytes,
        )

    def _fail_before_worktree(
        self, spec: TaskSpec, run_id: RunId, limits: Limits, routing: RunRouting, error: Exception
    ) -> RunReport:
        """End a claimed run whose worktree could not be made, with a report that says why."""
        report = RunReport(
            run_id=run_id,
            state=RunState.TERMINAL,
            terminal_reason=TerminalReason.FAILED,
            repo=spec.repo,
            base_commit=None,
            branch=None,
            commits_on_branch=0,
            worker_cli_version=None,
            worker_model=None,
            worker_exit_status=None,
            duration_s=0.0,
            limits_configured=limits,
            limit_hit=None,
            survivors=(),
            result_text="",
            notes=(f"the run workspace could not be prepared: {error}",),
            routing=routing,
        )
        self._store.finish(
            run_id,
            FinishRecord(
                TerminalReason.FAILED, None, 0, None, None, 0.0, self._renderer.render(report)
            ),
        )
        return report

    def _drive(self, job: "_Job", cleanup: "_Cleanup") -> RunReport:
        """Run the worker in the prepared workspace and store the outcome."""
        spec, run_id, prepared = job.spec, job.run_id, job.prepared
        limits, cli_version = job.limits, job.cli_version
        started = self._clock.now()
        if self._stops.stop_requested(run_id):
            return self._stopped_early(job)
        outcome, launch_error = self._execute(job)
        verdict = None if launch_error else self._worker.parse(outcome)
        succeeded = verdict is not None and verdict.succeeded
        result_text = verdict.result_text if verdict else ""
        self._store.set_state(run_id, RunState.FINALIZING)
        keep_sandbox = bool(outcome.survivors_alive)
        cleanup.keep_sandbox = keep_sandbox
        # A live process could still change files while they are read, so nothing is sealed.
        # A read task has no result branch to seal: it could not change the worktree.
        read_only = job.routing.mode is TaskMode.READ
        sealed = (
            SealedResult(branch=None, commits_on_branch=0)
            if keep_sandbox or read_only
            else self._workspace.seal(prepared)
        )
        reason = self._reason(outcome, launch_error, succeeded=succeeded)
        duration = self._clock.now() - started
        report = RunReport(
            run_id=run_id,
            state=RunState.TERMINAL,
            terminal_reason=reason,
            repo=spec.repo,
            base_commit=prepared.base_commit,
            branch=sealed.branch,
            commits_on_branch=sealed.commits_on_branch,
            worker_cli_version=cli_version,
            worker_model=verdict.model if verdict else None,
            worker_exit_status=outcome.exit_status,
            duration_s=duration,
            limits_configured=limits,
            limit_hit=outcome.limit_hit,
            survivors=outcome.survivors,
            result_text=result_text,
            notes=(
                *self._notes(
                    prepared, launch_error, sealed, keep_sandbox=keep_sandbox, read_only=read_only
                ),
                *(self._stop_notes(run_id) if outcome.stopped else ()),
            ),
            routing=job.routing,
        )
        self._store.finish(
            run_id,
            FinishRecord(
                reason,
                sealed.branch,
                sealed.commits_on_branch,
                outcome.limit_hit,
                outcome.exit_status,
                duration,
                self._renderer.render(report),
            ),
        )
        return report

    def _stopped_early(self, job: "_Job") -> RunReport:
        """End a run that was asked to stop before its worker started: nothing to seal."""
        self._store.set_state(job.run_id, RunState.STOPPING)
        report = RunReport(
            run_id=job.run_id,
            state=RunState.TERMINAL,
            terminal_reason=TerminalReason.STOPPED,
            repo=job.spec.repo,
            base_commit=job.prepared.base_commit,
            branch=None,
            commits_on_branch=0,
            worker_cli_version=job.cli_version,
            worker_model=None,
            worker_exit_status=None,
            duration_s=0.0,
            limits_configured=job.limits,
            limit_hit=None,
            survivors=(),
            result_text="",
            notes=(
                *job.prepared.notes,
                "stopped before the worker started",
                *self._stop_notes(job.run_id),
            ),
            routing=job.routing,
        )
        self._store.finish(
            job.run_id,
            FinishRecord(
                TerminalReason.STOPPED, None, 0, None, None, 0.0, self._renderer.render(report)
            ),
        )
        return report

    def _stop_notes(self, run_id: RunId) -> tuple[str, ...]:
        """Return the note that says why the run was stopped, when a reason was given."""
        reason = self._stops.stop_reason(run_id)
        return (f"stopped: {reason}",) if reason else ()

    def _notes(
        self,
        prepared: PreparedWorkspace,
        launch_error: LaunchError | None,
        sealed: SealedResult,
        *,
        keep_sandbox: bool,
        read_only: bool,
    ) -> tuple[str, ...]:
        """Collect the report notes in the order a reader needs them."""
        notes = list(prepared.notes)
        notes.extend(f"proxy: {fault}" for fault in self._egress.faults())
        if launch_error is not None:
            notes.append(f"worker did not start: {launch_error}")
        if keep_sandbox:
            notes.append("result not sealed: a worker process is still alive")
        elif read_only:
            notes.append("read task: no result branch")
        elif not sealed.branch:
            notes.append("worker made no changes")
        return tuple(notes)

    def _execute(self, job: "_Job") -> tuple[ExecutionOutcome, LaunchError | None]:
        """Run the worker behind the proxy; the proxy is stopped on every path."""
        spec, run_id, prepared, limits = job.spec, job.run_id, job.prepared, job.limits
        endpoint = self._egress.start(run_id)
        try:
            command = self._worker.command(spec.task, prepared, endpoint, _invocation(job.routing))
            self._store.set_state(run_id, RunState.RUNNING)
            outcome = self._confinement.run(
                command,
                prepared,
                endpoint,
                limits,
                lambda identity: self._store.set_process(run_id, identity),
                self._stop_poll(run_id),
                job.routing.mode,
            )
        except LaunchError as error:
            return ExecutionOutcome(exit_status=None, stdout=""), error
        finally:
            self._egress.stop()
        return outcome, None

    def _stop_poll(self, run_id: RunId) -> Callable[[], bool]:
        """Return the poll for the watch loop: it marks the run STOPPING on the first request."""
        marked = False

        def poll() -> bool:
            nonlocal marked
            if not self._stops.stop_requested(run_id):
                return False
            if not marked:
                self._store.set_state(run_id, RunState.STOPPING)
                marked = True
            return True

        return poll

    @staticmethod
    def _reason(
        outcome: ExecutionOutcome, launch_error: LaunchError | None, *, succeeded: bool
    ) -> TerminalReason:
        """Map what happened to the terminal reason; the order is the precedence."""
        if launch_error is not None:
            return TerminalReason.LAUNCH_LOST
        if outcome.limit_hit is not None:
            return TerminalReason.RESOURCE_LIMIT
        if outcome.stopped:
            return TerminalReason.STOPPED
        if outcome.survivors_alive or not succeeded:
            return TerminalReason.FAILED
        return TerminalReason.COMPLETED
