"""The RunTask use case: one coding task from a repository to a result branch."""

from agents_dispatcher.application.ports import (
    Clock,
    Confinement,
    EgressProxy,
    FinishRecord,
    IdGenerator,
    RunRecord,
    RunStore,
    Worker,
    Workspace,
)
from agents_dispatcher.domain.run import (
    Limits,
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
        """Run the task and return the report."""
        run_id = self._ids.new_run_id()
        limits = Limits(
            hard_s=spec.hard_s or self._defaults.hard_s,
            memory_bytes=spec.memory_bytes or self._defaults.memory_bytes,
        )
        prepared = self._workspace.prepare(spec.repo, run_id)
        cli_version = self._worker.cli_version()
        self._store.begin(
            RunRecord(run_id, spec.repo, prepared.base_commit, prepared.branch_name, cli_version)
        )
        started = self._clock.now()
        self._store.set_state(run_id, RunState.STARTING)
        endpoint = self._egress.start(run_id)
        command = self._worker.command(spec.task, prepared, endpoint)
        self._store.set_state(run_id, RunState.RUNNING)
        outcome = self._confinement.run(
            command, prepared, limits, lambda identity: self._store.set_process(run_id, identity)
        )
        self._egress.stop()
        verdict = self._worker.parse(outcome)
        self._store.set_state(run_id, RunState.FINALIZING)
        sealed = self._workspace.seal(prepared)
        duration = self._clock.now() - started
        self._store.finish(
            run_id,
            FinishRecord(
                TerminalReason.COMPLETED,
                sealed.branch,
                sealed.commits_on_branch,
                outcome.limit_hit,
                outcome.exit_status,
                duration,
            ),
        )
        self._workspace.retire(prepared, keep_sandbox=False)
        notes = prepared.notes if sealed.branch else (*prepared.notes, "worker made no changes")
        return RunReport(
            run_id=run_id,
            state=RunState.TERMINAL,
            terminal_reason=TerminalReason.COMPLETED,
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
            result_text=verdict.result_text,
            notes=notes,
        )
