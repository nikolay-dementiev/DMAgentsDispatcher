"""Task modes, provider links and the provider chain of a task type (ARCH L3533, L4211-4213).

This module is pure and names no vendor: a provider is an identifier the settings declare.
"""

import re
from dataclasses import dataclass, field
from enum import Enum

NAME_PATTERN = "[a-z][a-z0-9-]{0,31}"
_NAME = re.compile(NAME_PATTERN)


def validate_name(text: str, what: str) -> str:
    """Return `text` when it is a valid task type name or provider id; else raise `ValueError`.

    A name is lowercase letters, digits and hyphens, starts with a letter and has at most 32
    characters. `what` says which kind of name was checked, for the message.
    """
    if _NAME.fullmatch(text) is None:
        message = (
            f"{what} {text!r} is not valid: use lowercase letters, digits and hyphens, "
            "start with a letter, at most 32 characters"
        )
        raise ValueError(message)
    return text


class TaskMode(Enum):
    """Whether a task may write: read tasks cannot, write tasks deliver a result branch."""

    READ = "read"
    WRITE = "write"


class RouteMode(Enum):
    """How the caller asked: walk the whole chain, or evaluate one named provider only."""

    ROUTE = "ROUTE"
    START = "START"


@dataclass(frozen=True)
class ProviderLink:
    """One provider in a chain, with the model and effort it overrides for this task only."""

    provider: str
    model: str | None = None
    effort: str | None = None

    def __post_init__(self) -> None:
        """Refuse an id that is not a valid name."""
        validate_name(self.provider, "provider id")


@dataclass(frozen=True)
class HandoffLink:
    """The terminal link: when every execution link refuses, hand the task back as advice."""


Link = ProviderLink | HandoffLink


@dataclass(frozen=True)
class Chain:
    """An ordered chain of responsibility over provider links."""

    links: tuple[Link, ...]

    def __post_init__(self) -> None:
        """Refuse an empty chain, a repeated provider and a handoff that is not the last link."""
        if not self.links:
            message = "the chain is empty"
            raise ValueError(message)
        seen: set[str] = set()
        for position, link in enumerate(self.links):
            if isinstance(link, HandoffLink):
                if position != len(self.links) - 1:
                    message = "a handoff link is allowed only as the last link"
                    raise ValueError(message)
            elif link.provider in seen:
                message = f"provider {link.provider} appears more than once"
                raise ValueError(message)
            else:
                seen.add(link.provider)


@dataclass(frozen=True)
class ResolvedTask:
    """What a task type resolved to: its name (None when the defaults resolved it), mode, chain."""

    task_type: str | None
    mode: TaskMode
    chain: Chain

    def __post_init__(self) -> None:
        """Refuse a task type that is not a valid name."""
        if self.task_type is not None:
            validate_name(self.task_type, "task type")


class ProblemCode(Enum):
    """Why a link refused, or why the whole chain did (ARCH L1383-L1406)."""

    PROVIDER_NOT_LOADED = "PROVIDER_NOT_LOADED"
    PROVIDER_DISABLED = "PROVIDER_DISABLED"
    EXECUTABLE_UNAVAILABLE = "EXECUTABLE_UNAVAILABLE"
    CAPACITY_CONTENDED = "CAPACITY_CONTENDED"
    AUTH_REQUIRED = "AUTH_REQUIRED"
    LOGIN_LOST = "LOGIN_LOST"
    QUOTA_RESERVE = "QUOTA_RESERVE"
    QUOTA_UNSUPPORTED = "QUOTA_UNSUPPORTED"
    QUOTA_UNSUPPORTED_ALLOWED = "QUOTA_UNSUPPORTED_ALLOWED"
    QUOTA_STALE = "QUOTA_STALE"
    QUOTA_UNAVAILABLE = "QUOTA_UNAVAILABLE"
    QUOTA_UNKNOWN_ALLOWED = "QUOTA_UNKNOWN_ALLOWED"
    QUOTA_CONFLICT = "QUOTA_CONFLICT"
    ACCOUNT_CHANGED = "ACCOUNT_CHANGED"
    NO_PROVIDER_ELIGIBLE = "NO_PROVIDER_ELIGIBLE"
    AUTHORITY_VIOLATION = "AUTHORITY_VIOLATION"


@dataclass(frozen=True)
class Problem:
    """A refusal reason: a code for programs and a message for people."""

    code: ProblemCode
    message: str

    def as_dict(self) -> dict[str, object]:
        """Return the problem as plain values."""
        return {"code": self.code.value, "message": self.message}

    @staticmethod
    def from_dict(value: object) -> "Problem":
        """Read a problem back from `as_dict` values; raise `ValueError` for anything else."""
        if not isinstance(value, dict) or not isinstance(value.get("message"), str):
            message = f"not a problem: {value!r}"
            raise ValueError(message)  # noqa: TRY004 - a damaged value is a value error
        return Problem(ProblemCode(value.get("code")), value["message"])


class Disposition(Enum):
    """What one link answered."""

    SELECTED = "SELECTED"
    REFUSED = "REFUSED"


@dataclass(frozen=True)
class Evaluation:
    """One evaluated link (M2-provisional subset of ARCH `DecisionContextV1.evaluations`)."""

    provider: str
    disposition: Disposition
    problems: tuple[Problem, ...] = ()
    model: str | None = None
    effort: str | None = None
    warnings: tuple[Problem, ...] = ()
    evidence: dict[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Require a reason and no invocation facts for a refusal, the reverse for a selection.

        `warnings` are problems that did not stop a selected link; `evidence` is what the account
        observations read (login, quota), shown on a selection and on the refusal it explains.
        """
        if self.disposition is Disposition.REFUSED:
            if self.warnings:
                message = f"a refusal of {self.provider} carries no warnings"
                raise ValueError(message)
            if not self.problems:
                message = f"a refusal of {self.provider} must carry a problem"
                raise ValueError(message)
            if self.model is not None or self.effort is not None:
                message = f"a refusal of {self.provider} carries no model or effort"
                raise ValueError(message)
        else:
            if self.problems:
                message = f"a selected {self.provider} cannot carry a problem"
                raise ValueError(message)
            if self.model is None or self.effort is None:
                message = f"a selected {self.provider} needs a model and an effort"
                raise ValueError(message)

    def as_dict(self) -> dict[str, object]:
        """Return the evaluation as plain values in contract order."""
        value: dict[str, object] = {
            "provider": self.provider,
            "disposition": self.disposition.value,
            "problems": [problem.as_dict() for problem in self.problems],
        }
        if self.disposition is Disposition.SELECTED:
            value["model"] = self.model
            value["effort"] = self.effort
        if self.warnings:
            value["warnings"] = [warning.as_dict() for warning in self.warnings]
        if self.evidence:
            value["evidence"] = self.evidence
        return value

    @staticmethod
    def from_dict(value: object) -> "Evaluation":
        """Read an evaluation back from `as_dict` values; raise `ValueError` for anything else."""
        if not isinstance(value, dict) or not isinstance(value.get("provider"), str):
            message = f"not an evaluation: {value!r}"
            raise ValueError(message)  # noqa: TRY004 - a damaged value is a value error
        problems = value.get("problems", [])
        if not isinstance(problems, list):
            message = f"not an evaluation: {value!r}"
            raise ValueError(message)  # noqa: TRY004 - a damaged value is a value error
        warnings = value.get("warnings", [])
        evidence = value.get("evidence", {})
        if not isinstance(warnings, list) or not isinstance(evidence, dict):
            message = f"not an evaluation: {value!r}"
            raise ValueError(message)  # noqa: TRY004 - a damaged value is a value error
        return Evaluation(
            value["provider"],
            Disposition(value.get("disposition")),
            tuple(Problem.from_dict(item) for item in problems),
            model=value.get("model"),
            effort=value.get("effort"),
            warnings=tuple(Problem.from_dict(item) for item in warnings),
            evidence=evidence,
        )


BOUNDARY = "OUTSIDE_SDK_GUARANTEES"
NO_PROVIDER_MESSAGE = "every execution link in the chain refused the task"


@dataclass(frozen=True)
class Started:
    """The decision to run: the selected link and every link evaluated up to it."""

    selected: Evaluation
    evaluations: tuple[Evaluation, ...]

    @property
    def provider(self) -> str:
        """Return the provider that will run the task."""
        return self.selected.provider

    @property
    def model(self) -> str | None:
        """Return the model the run will use."""
        return self.selected.model

    @property
    def effort(self) -> str | None:
        """Return the effort the run will use."""
        return self.selected.effort


@dataclass(frozen=True)
class Recommendation:
    """Advice for the caller when no link ran the task and the chain ends in a handoff (Q-C)."""

    refusals: tuple[Evaluation, ...]
    model: str
    effort: str
    mode: TaskMode
    task_type: str | None

    def as_dict(self) -> dict[str, object]:
        """Return the recommendation as plain values; names follow ARCH `DecisionV1`."""
        return {
            "decision": "RECOMMENDATION",
            "task_type": self.task_type,
            "model": self.model,
            "effort": self.effort,
            "requirements": {"mode": self.mode.value},
            "refusals": [evaluation.as_dict() for evaluation in self.refusals],
            "boundary": BOUNDARY,
        }


@dataclass(frozen=True)
class Rejected:
    """The task was refused: no link took it and there is no handoff."""

    context: tuple[Evaluation, ...]
    task_type: str | None

    def as_dict(self) -> dict[str, object]:
        """Return the rejection as plain values; names follow ARCH `DecisionV1`."""
        problem = Problem(ProblemCode.NO_PROVIDER_ELIGIBLE, NO_PROVIDER_MESSAGE)
        return {
            "decision": "REJECTED",
            "task_type": self.task_type,
            "problems": [problem.as_dict()],
            "context": [evaluation.as_dict() for evaluation in self.context],
        }


Decision = Started | Recommendation | Rejected


@dataclass(frozen=True)
class RunRouting:
    """What a run keeps of its decision: the task, the provider and the route that chose it.

    `model` and `effort` are None only for a run stored before routing existed.
    """

    task_type: str | None
    mode: TaskMode
    provider: str
    model: str | None
    effort: str | None
    route: tuple[Evaluation, ...]

    @staticmethod
    def from_started(task: ResolvedTask, decision: Started) -> "RunRouting":
        """Build the routing of a run from the task and the decision to start it."""
        return RunRouting(
            task_type=task.task_type,
            mode=task.mode,
            provider=decision.provider,
            model=decision.model,
            effort=decision.effort,
            route=decision.evaluations,
        )
