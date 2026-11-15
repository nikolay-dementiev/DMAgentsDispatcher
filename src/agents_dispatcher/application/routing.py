"""The router: a chain of responsibility over provider links (ARCH L4211-L4213).

Each link is a handler that takes the task or passes it on; the walk stops at the first link that
takes it. The router knows no provider: implementations arrive as plugins from a composition root,
and the settings say which implementation each provider id uses.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol

from agents_dispatcher.application.errors import (
    InvalidRouteRequestError,
    UnknownTaskTypeError,
    WriteTaskNotAllowedError,
)
from agents_dispatcher.application.ports import Occupancy
from agents_dispatcher.application.settings import ProviderSettings, TaskDefinition
from agents_dispatcher.domain.routing import (
    Chain,
    Decision,
    Disposition,
    Evaluation,
    HandoffLink,
    Problem,
    ProblemCode,
    ProviderLink,
    Recommendation,
    Rejected,
    ResolvedTask,
    Started,
    TaskMode,
)


class ProviderPlugin(Protocol):
    """One registered provider implementation; it knows what its own tool needs."""

    def check(self, task: ResolvedTask) -> tuple[Problem, ...]:
        """Return the problems that make the provider refuse `task`; empty means eligible."""
        ...


@dataclass(frozen=True)
class GateResult:
    """What an account gate found for one link: refusals, warnings and the evidence read."""

    problems: tuple[Problem, ...] = ()
    warnings: tuple[Problem, ...] = ()
    evidence: dict[str, object] = field(default_factory=dict)


class AccountGate(Protocol):
    """Observes the account behind one implementation: its login, identity and quota."""

    def assess(self, provider: str, task: ResolvedTask) -> GateResult:
        """Return the problems that make the provider refuse `task`, and what was read."""
        ...


class TaskResolver:
    """Turns a task type name into a mode and a chain, and enforces who may write."""

    def __init__(
        self,
        tasks: Mapping[str, TaskDefinition],
        *,
        default_mode: TaskMode,
        default_chain: Chain,
        allow_write_tasks: tuple[str, ...],
    ) -> None:
        """Keep the task types, the defaults they inherit and the write allow-list."""
        self._tasks = tasks
        self._mode = default_mode
        self._chain = default_chain
        self._allowed = allow_write_tasks

    def resolve(self, task_type: str | None) -> ResolvedTask:
        """Resolve `task_type`; None resolves from the defaults alone.

        Raise `UnknownTaskTypeError` for a name no settings declare and
        `WriteTaskNotAllowedError` for a write task that the allow-list does not name.
        """
        if task_type is None:
            task = ResolvedTask(None, self._mode, self._chain)
        else:
            definition = self._tasks.get(task_type)
            if definition is None:
                message = f"unknown task type {task_type!r}; declare it as tasks.{task_type}"
                raise UnknownTaskTypeError(message)
            task = ResolvedTask(task_type, definition.mode, definition.chain or self._chain)
        if task.mode is TaskMode.WRITE and task.task_type not in self._allowed:
            name = task.task_type or "the defaults"
            message = f"task type {name} is a write task and is not in security.allow_write_tasks"
            raise WriteTaskNotAllowedError(message)
        return task


class Router:
    """Walks the chain of a resolved task and decides: start, recommend or reject."""

    def __init__(  # noqa: PLR0913 - the composition root wires exactly these collaborators
        self,
        plugins: Mapping[str, ProviderPlugin],
        providers: Mapping[str, ProviderSettings],
        *,
        occupancy: Occupancy,
        gates: Mapping[str, AccountGate],
        recommendation_model: str,
        recommendation_effort: str,
    ) -> None:
        """Keep the plugins by implementation name, the provider settings and the advice."""
        self._plugins = plugins
        self._providers = providers
        self._occupancy = occupancy
        self._gates = gates
        self._model = recommendation_model
        self._effort = recommendation_effort

    def route(
        self,
        task: ResolvedTask,
        *,
        only: str | None = None,
        contended: frozenset[str] = frozenset(),
    ) -> Decision:
        """Decide `task`; with `only` (the START mode) evaluate just that link, no handoff.

        Providers in `contended` were refused by the store a moment ago and are treated as full.
        """
        if only is not None and not self._in_chain(task, only):
            message = f"provider {only} is not in the chain of this task"
            raise InvalidRouteRequestError(message)
        evaluations: list[Evaluation] = []
        for link in task.chain.links:
            if isinstance(link, HandoffLink):
                if only is not None:
                    continue
                return Recommendation(
                    tuple(evaluations), self._model, self._effort, task.mode, task.task_type
                )
            if only is not None and link.provider != only:
                continue
            evaluation = self._evaluate(link, task, contended)
            evaluations.append(evaluation)
            if evaluation.disposition is Disposition.SELECTED:
                return Started(evaluation, tuple(evaluations))
        return Rejected(tuple(evaluations), task.task_type)

    @staticmethod
    def _in_chain(task: ResolvedTask, provider: str) -> bool:
        """Return whether `provider` has a link in the task's chain."""
        return any(
            isinstance(link, ProviderLink) and link.provider == provider
            for link in task.chain.links
        )

    def _evaluate(
        self, link: ProviderLink, task: ResolvedTask, contended: frozenset[str]
    ) -> Evaluation:
        """Answer for one link: the generic checks here, the provider's own through its plugin."""
        settings = self._providers[link.provider]
        plugin = self._plugins.get(settings.implementation)
        problems: list[Problem] = []
        if plugin is None:
            text = f"implementation {settings.implementation} of {link.provider} is not loaded"
            problems.append(Problem(ProblemCode.PROVIDER_NOT_LOADED, text))
        if not settings.enabled:
            text = f"provider {link.provider} is disabled (providers.{link.provider}.enabled)"
            problems.append(Problem(ProblemCode.PROVIDER_DISABLED, text))
        if plugin is not None and settings.enabled:
            problems.extend(plugin.check(task))
            if link.provider in contended or (
                self._occupancy.active(link.provider) >= settings.capacity
            ):
                text = (
                    f"provider {link.provider} holds its capacity of {settings.capacity} "
                    f"non-terminal runs (providers.{link.provider}.capacity)"
                )
                problems.append(Problem(ProblemCode.CAPACITY_CONTENDED, text))
        gate = self._gates.get(settings.implementation)
        result = GateResult()
        if gate is not None and not problems and plugin is not None and settings.enabled:
            result = gate.assess(link.provider, task)
            problems.extend(result.problems)
        if problems:
            return Evaluation(
                link.provider, Disposition.REFUSED, tuple(problems), evidence=result.evidence
            )
        return Evaluation(
            link.provider,
            Disposition.SELECTED,
            model=link.model or settings.model,
            effort=link.effort or settings.effort,
            warnings=result.warnings,
            evidence=result.evidence,
        )
