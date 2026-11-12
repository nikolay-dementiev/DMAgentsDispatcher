"""The RunTask use case: one coding task from a repository to a result branch."""

from agents_dispatcher.application.errors import LaunchError
from agents_dispatcher.application.ports import (
    Clock,
    Confinement,
    EgressProxy,
    ExecutionOutcome,
    FinishRecord,
    IdGenerator,
    PreparedWorkspace,
    RunRecord,
    RunStore,
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
        self._defaults = default_limits

    def run(self, spec: TaskSpec) -> RunReport:
        """Run the task and return the report; a non-Git repository raises before any state."""
        run_id = self._ids.new_run_id()
        limits = Limits(
            hard_s=spec.hard_s or self._defaults.hard_s,
            memory_bytes=spec.memory_bytes or self._defaults.memory_bytes,
        )
        prepared = self._workspace.prepare(spec.repo, run_id)
        keep_sandbox = False
        try:
            cli_version = self._worker.cli_version()
            self._store.begin(
                RunRecord(
                    run_id, spec.repo, prepared.base_commit, prepared.branch_name, cli_version
                )
            )
            started = self._clock.now()
            self._store.set_state(run_id, RunState.STARTING)
            outcome, launch_error = self._execute(spec, run_id, prepared, limits)
            verdict = None if launch_error else self._worker.parse(outcome)
            succeeded = verdict is not None and verdict.succeeded
            result_text = verdict.result_text if verdict else ""
            self._store.set_state(run_id, RunState.FINALIZING)
            sealed = self._workspace.seal(prepared)
            keep_sandbox = bool(outcome.survivors_alive)
            reason = self._reason(outcome, launch_error, succeeded=succeeded)
            duration = self._clock.now() - started
            self._store.finish(
                run_id,
                FinishRecord(
                    reason,
                    sealed.branch,
                    sealed.commits_on_branch,
                    outcome.limit_hit,
                    outcome.exit_status,
                    duration,
                ),
            )
        finally:
            self._workspace.retire(prepared, keep_sandbox=keep_sandbox)
        notes = list(prepared.notes)
        if launch_error is not None:
            notes.append(f"worker did not start: {launch_error}")
        if not sealed.branch:
            notes.append("worker made no changes")
        return RunReport(
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
            notes=tuple(notes),
        )

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
