"""A routing for tests that accept runs: a write task that anthropic took, openai refused."""

from agents_dispatcher.domain.routing import (
    Disposition,
    Evaluation,
    Problem,
    ProblemCode,
    RunRouting,
    TaskMode,
)

MODEL = "claude-sonnet-5-5"
EFFORT = "medium"
REFUSED = Evaluation(
    "openai",
    Disposition.REFUSED,
    (Problem(ProblemCode.PROVIDER_NOT_LOADED, "implementation builtin.codex is not loaded"),),
)
SELECTED = Evaluation("anthropic", Disposition.SELECTED, model=MODEL, effort=EFFORT)
ROUTING = RunRouting("implement", TaskMode.WRITE, "anthropic", MODEL, EFFORT, (REFUSED, SELECTED))
