"""Quota admission: what a provider's account evidence allows (ARCH L4256-L4264).

This module is pure. Evidence arrives already read; the verdict says admit, refuse or admit with a
warning, and carries the facts the route shows. Quota is an observation at admission time, not
reserved usage and not a spending prediction.
"""

import math
from dataclasses import dataclass, field
from enum import Enum

from agents_dispatcher.domain.routing import Problem, ProblemCode

PERCENT_MAX = 100.0


class QuotaMode(Enum):
    """How strictly quota evidence gates admission (`defaults.quota_mode`)."""

    REQUIRED = "required"
    BEST_EFFORT = "best_effort"
    OFF = "off"


class EvidenceKind(Enum):
    """What the account interface gave back."""

    SNAPSHOT = "SNAPSHOT"
    UNSUPPORTED = "UNSUPPORTED"
    UNAVAILABLE = "UNAVAILABLE"
    CONFLICT = "CONFLICT"
    ACCOUNT_CHANGED = "ACCOUNT_CHANGED"


@dataclass(frozen=True)
class QuotaWindow:
    """One rate-limit window: its length in minutes and the percent of it already used."""

    minutes: int
    used_percent: float


@dataclass(frozen=True)
class QuotaEvidence:
    """One observation of an account's quota; `age_s` is how old the snapshot is."""

    kind: EvidenceKind
    windows: tuple[QuotaWindow, ...] = ()
    age_s: float | None = None
    detail: str = ""


@dataclass(frozen=True)
class QuotaVerdict:
    """The decision for one link: a refusal, or an admission that may carry a warning."""

    refusal: Problem | None
    warning: Problem | None
    facts: dict[str, object] = field(default_factory=dict)


def judge_quota(
    mode: QuotaMode,
    reserve_percent: int,
    max_age_s: float,
    evidence: QuotaEvidence | None,
) -> QuotaVerdict:
    """Apply the evidence table of the architecture to one observation (None = nothing read)."""
    facts: dict[str, object] = {"mode": mode.value, "reserve_percent": reserve_percent}
    if mode is QuotaMode.OFF:
        if evidence is not None and evidence.kind is EvidenceKind.ACCOUNT_CHANGED:
            return _refuse(facts, ProblemCode.ACCOUNT_CHANGED, "the account changed")
        return QuotaVerdict(None, None, {**facts, "result": "SKIPPED"})
    seen = evidence or QuotaEvidence(EvidenceKind.UNAVAILABLE, detail="no quota evidence was read")
    if seen.kind is EvidenceKind.SNAPSHOT:
        return _judge_snapshot(mode, reserve_percent, max_age_s, seen, facts)
    return _judge_without_snapshot(mode, seen, facts)


def _judge_without_snapshot(
    mode: QuotaMode, evidence: QuotaEvidence, facts: dict[str, object]
) -> QuotaVerdict:
    """Judge an observation that carries no readings."""
    kind = evidence.kind
    if kind is EvidenceKind.ACCOUNT_CHANGED:
        return _refuse(facts, ProblemCode.ACCOUNT_CHANGED, evidence.detail or "the account changed")
    if kind is EvidenceKind.CONFLICT:
        return _refuse(
            facts, ProblemCode.QUOTA_CONFLICT, evidence.detail or "observations conflict"
        )
    if kind is EvidenceKind.UNSUPPORTED:
        return _unsupported(mode, facts)
    reason = evidence.detail or "the quota could not be read"
    return _missing(mode, facts, ProblemCode.QUOTA_UNAVAILABLE, reason)


def _judge_snapshot(
    mode: QuotaMode,
    reserve_percent: int,
    max_age_s: float,
    evidence: QuotaEvidence,
    facts: dict[str, object],
) -> QuotaVerdict:
    """Judge a snapshot: shape first, then freshness, then the threshold."""
    windows = evidence.windows
    minutes = [window.minutes for window in windows]
    if len(set(minutes)) != len(minutes):
        return _refuse(facts, ProblemCode.QUOTA_CONFLICT, "two readings for the same window")
    if not windows or not all(_sound(window) for window in windows):
        return _missing(mode, facts, ProblemCode.QUOTA_UNAVAILABLE, "the snapshot is not usable")
    shown = {
        **facts,
        "age_s": evidence.age_s,
        "windows": [{"minutes": w.minutes, "used_percent": w.used_percent} for w in windows],
    }
    age = evidence.age_s
    if age is None or not math.isfinite(age) or age < 0:
        return _missing(mode, shown, ProblemCode.QUOTA_UNAVAILABLE, "the snapshot has no valid age")
    if age >= max_age_s:
        return _missing(mode, shown, ProblemCode.QUOTA_STALE, f"the snapshot is {age:g} s old")
    over = [w for w in windows if w.used_percent + reserve_percent >= PERCENT_MAX]
    if over:
        worst = max(over, key=lambda window: window.used_percent)
        text = (
            f"the {worst.minutes}-minute window is {worst.used_percent:g}% used, "
            f"with a reserve of {reserve_percent}%"
        )
        return _refuse(shown, ProblemCode.QUOTA_RESERVE, text)
    return QuotaVerdict(None, None, {**shown, "result": "ADMITTED"})


def _sound(window: QuotaWindow) -> bool:
    """Return whether a window has a positive length and a finite percent in range."""
    percent = window.used_percent
    return window.minutes > 0 and math.isfinite(percent) and 0 <= percent <= PERCENT_MAX


def _refuse(facts: dict[str, object], code: ProblemCode, text: str) -> QuotaVerdict:
    """Build a refusal that keeps the facts read so far."""
    return QuotaVerdict(Problem(code, text), None, {**facts, "result": "REFUSED"})


def _unsupported(mode: QuotaMode, facts: dict[str, object]) -> QuotaVerdict:
    """No quota interface: refused when required, admitted with a warning otherwise."""
    if mode is QuotaMode.REQUIRED:
        return _refuse(facts, ProblemCode.QUOTA_UNSUPPORTED, "the provider has no quota interface")
    warning = Problem(
        ProblemCode.QUOTA_UNSUPPORTED_ALLOWED,
        "the provider has no quota interface; admitted because quota_mode is best_effort",
    )
    return QuotaVerdict(None, warning, {**facts, "result": "UNSUPPORTED_ALLOWED"})


def _missing(
    mode: QuotaMode, facts: dict[str, object], code: ProblemCode, text: str
) -> QuotaVerdict:
    """Missing, unreadable or stale evidence: refused when required, allowed otherwise."""
    if mode is QuotaMode.REQUIRED:
        return _refuse(facts, code, text)
    warning = Problem(
        ProblemCode.QUOTA_UNKNOWN_ALLOWED,
        f"{text}; admitted because quota_mode is best_effort",
    )
    return QuotaVerdict(None, warning, {**facts, "result": "UNKNOWN_ALLOWED"})
