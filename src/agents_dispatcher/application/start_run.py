"""The `start` use case: accept a run, launch its supervisor, wait for the acknowledgement."""

from collections.abc import Callable, Mapping

from agents_dispatcher.application.errors import (
    CapacityContendedError,
    InputTooLargeError,
    LaunchError,
    LaunchLostError,
    NoProviderEligibleError,
    StartCancelledError,
    StartInterruptedError,
)
from agents_dispatcher.application.ports import (
    AcceptedRun,
    Clock,
    DecisionLog,
    FinishRecord,
    IdGenerator,
    ReportRenderer,
    RepositoryCheck,
    RunSweeper,
    Sleeper,
    StartStore,
    SupervisorLauncher,
    TaskResolving,
    TaskRouting,
)
from agents_dispatcher.domain.routing import Recommendation, Rejected, RouteMode, RunRouting
from agents_dispatcher.domain.run import (
    Limits,
    RunId,
    RunReport,
    RunState,
    TaskSpec,
    TerminalReason,
)
from agents_dispatcher.domain.views import RunSummary, bounded_text

StartOutcome = RunSummary | Recommendation


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
        resolver: TaskResolving,
        router: TaskRouting,
        decisions: DecisionLog,
        capacities: Mapping[str, int],
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
        self._resolver = resolver
        self._router = router
        self._decisions = decisions
        self._capacities = capacities

    def start(self, spec: TaskSpec, should_stop: Callable[[], bool] | None = None) -> StartOutcome:
        """Decide the task and start its run; raise `LaunchLostError` when no supervisor took over.

        Nothing is created before the decision is made and logged: a task over the input bound,
        an unknown or forbidden task type, a repository that is not a work tree, a provider the
        chain cannot have and a decision that cannot be logged all end the call first. A
        recommendation is returned as it is; a rejection is raised. Only a started decision
        accepts and launches; abandoned runs are swept before the route counts the capacity.
        `should_stop` is asked after the decision is logged and before the run is accepted: when it
        says yes, `StartCancelledError` is raised and no run exists.
        """
        size = len(spec.task.encode("utf-8"))
        if size > self._input_bytes:
            message = f"the task is {size} bytes; the bound is {self._input_bytes} bytes"
            raise InputTooLargeError(message)
        task = self._resolver.resolve(spec.task_type)
        self._repository.require_git(spec.repo)
        mode = RouteMode.ROUTE if spec.provider is None else RouteMode.START
        contended: set[str] = set()
        # Runs whose starter or supervisor died are closed first: their slots count as capacity.
        self._sweeper.sweep()
        while True:
            decision = self._router.route(task, only=spec.provider, contended=frozenset(contended))
            self._decisions.record(task, decision, mode)
            if isinstance(decision, Rejected):
                raise NoProviderEligibleError(decision)
            if isinstance(decision, Recommendation):
                return decision
            if should_stop is not None and should_stop():
                message = "the caller cancelled before the run was committed"
                raise StartCancelledError(message)
            run_id = self._ids.new_run_id()
            limits = Limits(
                hard_s=spec.hard_s or self._defaults.hard_s,
                memory_bytes=spec.memory_bytes or self._defaults.memory_bytes,
            )
            routing = RunRouting.from_started(task, decision)
            try:
                self._store.accept(
                    AcceptedRun(run_id, spec.repo, spec.task, limits, routing),
                    capacity=self._capacities[routing.provider],
                )
            except CapacityContendedError as error:
                if error.provider in contended:
                    raise NoProviderEligibleError(
                        Rejected(decision.evaluations, task.task_type)
                    ) from error
                contended.add(error.provider)
                continue
            break
        try:
            return self._take_over(run_id, spec, limits, routing)
        except KeyboardInterrupt as error:
            message = f"interrupted while the supervisor was taking over run {run_id.value}"
            raise StartInterruptedError(run_id, message) from error

    def _take_over(
        self, run_id: RunId, spec: TaskSpec, limits: Limits, routing: RunRouting
    ) -> RunSummary:
        """Launch the supervisor and wait for it to claim the run, or end the run as lost."""
        try:
            self._launcher.launch(run_id, spec.task)
        except LaunchError as error:
            return self._lost(
                run_id, spec, limits, routing, f"the supervisor could not be started: {error}"
            )
        claimed, fault = self._await_claim(run_id)
        if claimed:
            return self._summary(run_id)
        if fault:
            return self._lost(run_id, spec, limits, routing, f"the supervisor gave up: {fault}")
        note = f"the supervisor did not take over the run within {self._startup_s:g} s"
        return self._lost(run_id, spec, limits, routing, note)

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

    def _lost(
        self, run_id: RunId, spec: TaskSpec, limits: Limits, routing: RunRouting, note: str
    ) -> RunSummary:
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
            worker_model_source=None,
            worker_exit_status=None,
            duration_s=0.0,
            limits_configured=limits,
            limit_hit=None,
            survivors=(),
            result_text="",
            notes=(note,),
            routing=routing,
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
            routing=found.routing,
        )
