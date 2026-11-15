"""The router: a chain of responsibility over provider links (ARCH L4211-L4213).

Each link is a handler that takes the task or passes it on; the walk stops at the first link that
takes it. The router knows no provider: implementations arrive as plugins from a composition root,
and the settings say which implementation each provider id uses.
"""

from collections.abc import Mapping
from typing import Protocol

from agents_dispatcher.application.errors import InvalidRouteRequestError
from agents_dispatcher.application.settings import ProviderSettings
from agents_dispatcher.domain.routing import (
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
)


class ProviderPlugin(Protocol):
    """One registered provider implementation; it knows what its own tool needs."""

    def check(self, task: ResolvedTask) -> tuple[Problem, ...]:
        """Return the problems that make the provider refuse `task`; empty means eligible."""
        ...


class Router:
    """Walks the chain of a resolved task and decides: start, recommend or reject."""

    def __init__(
        self,
        plugins: Mapping[str, ProviderPlugin],
        providers: Mapping[str, ProviderSettings],
        *,
        recommendation_model: str,
        recommendation_effort: str,
    ) -> None:
        """Keep the plugins by implementation name, the provider settings and the advice."""
        self._plugins = plugins
        self._providers = providers
        self._model = recommendation_model
        self._effort = recommendation_effort

    def route(self, task: ResolvedTask, *, only: str | None = None) -> Decision:
        """Decide `task`; with `only` (the START mode) evaluate just that link, no handoff."""
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
            evaluation = self._evaluate(link, task)
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

    def _evaluate(self, link: ProviderLink, task: ResolvedTask) -> Evaluation:
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
        if problems:
            return Evaluation(link.provider, Disposition.REFUSED, tuple(problems))
        return Evaluation(
            link.provider,
            Disposition.SELECTED,
            model=link.model or settings.model,
            effort=link.effort or settings.effort,
        )
