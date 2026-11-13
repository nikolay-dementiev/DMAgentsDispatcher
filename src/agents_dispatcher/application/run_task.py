"""The RunTask use case: one coding task from a repository to a result branch."""

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
    IdGenerator,
    OwnerRegistry,
    Ownership,
    PreparedWorkspace,
    ProcessIdentity,
    ReportRenderer,
    RunRecord,
    RunStore,
    SealedResult,
    Worker,
    Workspace,
)
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


@dataclass
class _Cleanup:
    """What the entry point needs to know to clean up, set while the run proceeds."""

    keep_sandbox: bool = False


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
        ids: IdGenerator,
        owners: OwnerRegistry,
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
        self._ids = ids
        self._owners = owners
        self._renderer = renderer
        self._defaults = default_limits

    def run(self, spec: TaskSpec) -> RunReport:
        """Run the task and return the report; a non-Git repository raises before any state."""
        run_id = self._ids.new_run_id()
        limits = self._limits(spec)
        prepared = self._workspace.prepare(spec.repo, run_id)
        ownership: Ownership | None = None
        cleanup = _Cleanup()
        try:
            ownership = self._owners.hold(run_id)
            cli_version = self._worker.cli_version()
            self._store.begin(
                RunRecord(
                    run_id, spec.repo, prepared.base_commit, prepared.branch_name, cli_version
                )
            )
            self._store.set_state(run_id, RunState.STARTING)
            return self._drive(_Job(spec, run_id, prepared, limits, cli_version), cleanup)
        finally:
            self._workspace.retire(prepared, keep_sandbox=cleanup.keep_sandbox)
            if ownership is not None:
                ownership.release()

    def supervise(self, spec: TaskSpec, run_id: RunId, supervisor: ProcessIdentity) -> RunReport:
        """Adopt the run that `start` accepted, run it to its end and return the report.

        The claim comes first: it is the acknowledgement `start` waits for. A worktree that
        cannot be made ends the run as FAILED with a report instead of leaving it in STARTING.
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
                return self._fail_before_worktree(spec, run_id, limits, error)
            cleanup = _Cleanup()
            try:
                cli_version = self._worker.cli_version()
                self._store.record_workspace(
                    run_id, base_commit=prepared.base_commit, cli_version=cli_version
                )
                return self._drive(_Job(spec, run_id, prepared, limits, cli_version), cleanup)
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
        self, spec: TaskSpec, run_id: RunId, limits: Limits, error: Exception
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
            worker_exit_status=None,
            duration_s=0.0,
            limits_configured=limits,
            limit_hit=None,
            survivors=(),
            result_text="",
            notes=(f"the run workspace could not be prepared: {error}",),
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
        outcome, launch_error = self._execute(spec, run_id, prepared, limits)
        verdict = None if launch_error else self._worker.parse(outcome)
        succeeded = verdict is not None and verdict.succeeded
        result_text = verdict.result_text if verdict else ""
        self._store.set_state(run_id, RunState.FINALIZING)
        keep_sandbox = bool(outcome.survivors_alive)
        cleanup.keep_sandbox = keep_sandbox
        # A live process could still change files while they are read, so nothing is sealed.
        sealed = (
            SealedResult(branch=None, commits_on_branch=0)
            if keep_sandbox
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
            worker_exit_status=outcome.exit_status,
            duration_s=duration,
            limits_configured=limits,
            limit_hit=outcome.limit_hit,
            survivors=outcome.survivors,
            result_text=result_text,
            notes=self._notes(prepared, launch_error, sealed, keep_sandbox=keep_sandbox),
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

    def _notes(
        self,
        prepared: PreparedWorkspace,
        launch_error: LaunchError | None,
        sealed: SealedResult,
        *,
        keep_sandbox: bool,
    ) -> tuple[str, ...]:
        """Collect the report notes in the order a reader needs them."""
        notes = list(prepared.notes)
        notes.extend(f"proxy: {fault}" for fault in self._egress.faults())
        if launch_error is not None:
            notes.append(f"worker did not start: {launch_error}")
        if keep_sandbox:
            notes.append("result not sealed: a worker process is still alive")
        elif not sealed.branch:
            notes.append("worker made no changes")
        return tuple(notes)

    def _execute(
        self, spec: TaskSpec, run_id: RunId, prepared: PreparedWorkspace, limits: Limits
    ) -> tuple[ExecutionOutcome, LaunchError | None]:
        """Run the worker behind the proxy; the proxy is stopped on every path."""
        endpoint = self._egress.start(run_id)
        try:
            command = self._worker.command(spec.task, prepared, endpoint)
            self._store.set_state(run_id, RunState.RUNNING)
            outcome = self._confinement.run(
                command,
                prepared,
                endpoint,
                limits,
                lambda identity: self._store.set_process(run_id, identity),
            )
        except LaunchError as error:
            return ExecutionOutcome(exit_status=None, stdout=""), error
        finally:
            self._egress.stop()
        return outcome, None

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
