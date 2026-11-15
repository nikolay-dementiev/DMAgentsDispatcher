"""A routing for tests that accept runs: a write task that anthropic took, openai refused."""

from agents_dispatcher.domain.routing import (
    Chain,
    Decision,
    Disposition,
    Evaluation,
    Problem,
    ProblemCode,
    ProviderLink,
    ResolvedTask,
    RouteMode,
    RunRouting,
    Started,
    TaskMode,
)
from tests.support.run_spies import Journal

MODEL = "claude-sonnet-5-5"
EFFORT = "medium"
REFUSED = Evaluation(
    "openai",
    Disposition.REFUSED,
    (Problem(ProblemCode.PROVIDER_NOT_LOADED, "implementation builtin.codex is not loaded"),),
)
SELECTED = Evaluation("anthropic", Disposition.SELECTED, model=MODEL, effort=EFFORT)
ROUTING = RunRouting("implement", TaskMode.WRITE, "anthropic", MODEL, EFFORT, (REFUSED, SELECTED))
STARTED = Started(SELECTED, (REFUSED, SELECTED))
TASK = ResolvedTask("implement", TaskMode.WRITE, Chain((ProviderLink("anthropic"),)))


class FixedResolver:
    """Resolves every task type to `task`, or fails with `error`."""

    def __init__(self, task: ResolvedTask = TASK) -> None:
        """Keep the task and the failure to raise, if any."""
        self.task = task
        self.error: Exception | None = None
        self.asked: list[str | None] = []

    def resolve(self, task_type: str | None) -> ResolvedTask:
        """Return the task, or raise the configured error."""
        self.asked.append(task_type)
        if self.error is not None:
            raise self.error
        return self.task


class FixedRouter:
    """Decides every task with one canned decision and records what it was asked."""

    def __init__(self, journal: Journal) -> None:
        """Keep the journal and the canned decision."""
        self.journal = journal
        self.decision: Decision = STARTED
        self.error: Exception | None = None
        self.only: list[str | None] = []
        self.tasks: list[ResolvedTask] = []

    def route(self, task: ResolvedTask, *, only: str | None = None) -> Decision:
        """Record the call and return the canned decision."""
        self.journal.add("route")
        self.tasks.append(task)
        self.only.append(only)
        if self.error is not None:
            raise self.error
        return self.decision


class SpyDecisions:
    """The decision log: records each decision with the mode of the request."""

    def __init__(self, journal: Journal) -> None:
        """Keep the journal."""
        self.journal = journal
        self.recorded: list[tuple[ResolvedTask, Decision, RouteMode]] = []
        self.error: Exception | None = None

    def record(self, task: ResolvedTask, decision: Decision, mode: RouteMode) -> None:
        """Record the decision, or fail like a log that cannot be written."""
        self.journal.add("decision.log")
        if self.error is not None:
            raise self.error
        self.recorded.append((task, decision, mode))
