"""The `start` use case: accept a run, launch its supervisor, wait for the acknowledgement."""

from agents_dispatcher.application.errors import (
    InputTooLargeError,
    LaunchError,
    LaunchLostError,
)
from agents_dispatcher.application.ports import (
    AcceptedRun,
    Clock,
    FinishRecord,
    IdGenerator,
    ReportRenderer,
    RepositoryCheck,
    RunSweeper,
    Sleeper,
    StartStore,
    SupervisorLauncher,
)
from agents_dispatcher.domain.run import (
    Limits,
    RunId,
    RunReport,
    RunState,
    TaskSpec,
    TerminalReason,
)
from agents_dispatcher.domain.views import RunSummary, bounded_text


class StartRun:
    """Creates a run and hands it to a detached supervisor; returns once that took over."""

    def __init__(  # noqa: PLR0913 - the composition root wires exactly these collaborators
        self,
        *,
        repository: RepositoryCheck,
        ids: IdGenerator,
        store: StartStore,
        sweeper: RunSweeper,
        launcher: SupervisorLauncher,
        clock: Clock,
        sleeper: Sleeper,
        renderer: ReportRenderer,
        default_limits: Limits,
        startup_s: float,
        scan_s: float,
        summary_bytes: int,
        input_bytes: int,
    ) -> None:
        """Keep the ports and the settings this use case consumes."""
        self._repository = repository
        self._ids = ids
        self._store = store
        self._sweeper = sweeper
        self._launcher = launcher
        self._clock = clock
        self._sleeper = sleeper
        self._renderer = renderer
        self._defaults = default_limits
        self._startup_s = startup_s
        self._scan_s = scan_s
        self._summary_bytes = summary_bytes
        self._input_bytes = input_bytes

    def start(self, spec: TaskSpec) -> RunSummary:
        """Start the run; raise `LaunchLostError` when the supervisor never took over.

        A task over the input bound is refused first, before anything is looked at or created.
        """
        size = len(spec.task.encode("utf-8"))
        if size > self._input_bytes:
            message = f"the task is {size} bytes; the bound is {self._input_bytes} bytes"
            raise InputTooLargeError(message)
        self._repository.require_git(spec.repo)
        self._sweeper.sweep()  # runs whose starter or supervisor died are closed on the way
        run_id = self._ids.new_run_id()
        limits = Limits(
            hard_s=spec.hard_s or self._defaults.hard_s,
            memory_bytes=spec.memory_bytes or self._defaults.memory_bytes,
        )
        self._store.accept(AcceptedRun(run_id, spec.repo, spec.task, limits))
        try:
            self._launcher.launch(run_id, spec.task)
        except LaunchError as error:
            return self._lost(run_id, spec, limits, f"the supervisor could not be started: {error}")
        claimed, fault = self._await_claim(run_id)
        if claimed:
            return self._summary(run_id)
        if fault:
            return self._lost(run_id, spec, limits, f"the supervisor gave up: {fault}")
        note = f"the supervisor did not take over the run within {self._startup_s:g} s"
        return self._lost(run_id, spec, limits, note)

    def _await_claim(self, run_id: RunId) -> tuple[bool, str | None]:
        """Poll until the run is claimed, a fault is reported or the start-up bound passes."""
        deadline = self._clock.now() + self._startup_s
        while True:
            found = self._store.find(run_id)
            if found is not None and found.state is not RunState.ACCEPTED:
                return True, None
            if found is not None and found.fault:
                return False, found.fault
            left = deadline - self._clock.now()
            if left <= 0:
                return False, None
            self._sleeper.sleep(min(self._scan_s, left))

    def _lost(self, run_id: RunId, spec: TaskSpec, limits: Limits, note: str) -> RunSummary:
        """End an unclaimed run as LAUNCH_LOST; a supervisor that just claimed it wins."""
        report = RunReport(
            run_id=run_id,
            state=RunState.TERMINAL,
            terminal_reason=TerminalReason.LAUNCH_LOST,
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
            notes=(note,),
        )
        record = FinishRecord(
            TerminalReason.LAUNCH_LOST, None, 0, None, None, 0.0, self._renderer.render(report)
        )
        if not self._store.finish_unclaimed(run_id, record):
            return self._summary(run_id)
        raise LaunchLostError(run_id, note)

    def _summary(self, run_id: RunId) -> RunSummary:
        """Describe the run as it is now."""
        found = self._store.find(run_id)
        if found is None:
            message = f"run {run_id.value} vanished from the store right after it was accepted"
            raise LaunchLostError(run_id, message)
        return RunSummary(
            run_id=run_id,
            task=bounded_text(found.task, self._summary_bytes),
            state=found.state,
            reason=found.terminal_reason,
            branch=found.branch,
        )
