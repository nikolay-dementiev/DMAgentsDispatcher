"""Tests of the quota admission rules (ARCH L4256-L4264): evidence, mode, reserve and freshness."""

import math
import unittest

from agents_dispatcher.domain.quota import (
    EvidenceKind,
    QuotaEvidence,
    QuotaMode,
    QuotaVerdict,
    QuotaWindow,
    judge_quota,
)
from agents_dispatcher.domain.routing import ProblemCode

RESERVE = 15
MAX_AGE = 30.0
FIVE_HOURS = 300
WEEK = 10080


def snapshot(*windows: QuotaWindow, age_s: float | None = 1.0) -> QuotaEvidence:
    """Return a snapshot as the account reported it."""
    return QuotaEvidence(EvidenceKind.SNAPSHOT, windows=windows, age_s=age_s)


def usable(five: float = 10.0, week: float = 20.0) -> QuotaEvidence:
    """Return a fresh two-window snapshot."""
    return snapshot(QuotaWindow(FIVE_HOURS, five), QuotaWindow(WEEK, week))


def refusal_code(verdict: QuotaVerdict) -> ProblemCode | None:
    """Return the code of the refusal, or None for an admission."""
    return verdict.refusal.code if verdict.refusal else None


def warning_code(verdict: QuotaVerdict) -> ProblemCode | None:
    """Return the code of the warning, or None."""
    return verdict.warning.code if verdict.warning else None


def judge(mode: QuotaMode, evidence: QuotaEvidence | None) -> QuotaVerdict:
    """Judge with the shipped reserve and freshness bound."""
    return judge_quota(mode, RESERVE, MAX_AGE, evidence)


class FreshEvidenceTests(unittest.TestCase):
    """A usable snapshot below the threshold admits in every mode that looks."""

    def test_below_the_threshold_admits_without_a_warning(self) -> None:
        """Required and best effort both admit; nothing to warn about."""
        for mode in (QuotaMode.REQUIRED, QuotaMode.BEST_EFFORT):
            with self.subTest(mode=mode):
                verdict = judge(mode, usable())
                self.assertIsNone(verdict.refusal, msg="admitted")
                self.assertIsNone(verdict.warning, msg="no warning")

    def test_the_facts_show_what_was_read(self) -> None:
        """The route carries the windows, the age and the reserve."""
        facts = judge(QuotaMode.REQUIRED, usable(12.5, 40.0)).facts
        self.assertEqual(facts["result"], "ADMITTED", msg="result")
        self.assertEqual(facts["age_s"], 1.0, msg="age")
        self.assertEqual(facts["reserve_percent"], RESERVE, msg="reserve")
        self.assertEqual(
            facts["windows"],
            [
                {"minutes": FIVE_HOURS, "used_percent": 12.5},
                {"minutes": WEEK, "used_percent": 40.0},
            ],
            msg="windows in the order read",
        )

    def test_the_threshold_is_strict_used_plus_reserve_must_stay_below_100(self) -> None:
        """85 + 15 = 100 is already a limit; 84.9 is not."""
        edge = judge(QuotaMode.REQUIRED, usable(five=85.0))
        below = judge(QuotaMode.REQUIRED, usable(five=84.9))
        self.assertEqual(refusal_code(edge), ProblemCode.QUOTA_RESERVE, msg="85 + 15 refused")
        self.assertIsNone(below.refusal, msg="84.9 + 15 admitted")

    def test_any_window_over_the_threshold_refuses_in_every_mode(self) -> None:
        """The week window decides even when the five-hour one is empty."""
        for mode in (QuotaMode.REQUIRED, QuotaMode.BEST_EFFORT):
            with self.subTest(mode=mode):
                verdict = judge(mode, usable(five=0.0, week=90.0))
                self.assertEqual(refusal_code(verdict), ProblemCode.QUOTA_RESERVE, msg="refused")
                self.assertEqual(verdict.facts["result"], "REFUSED", msg="fact")


class ModeOffTests(unittest.TestCase):
    """`off` skips quota admission; account consistency stays."""

    def test_off_skips_whatever_the_evidence_says(self) -> None:
        """Even an exhausted account is not looked at."""
        for evidence in (usable(five=99.0), None, QuotaEvidence(EvidenceKind.UNSUPPORTED)):
            with self.subTest(evidence=evidence):
                verdict = judge(QuotaMode.OFF, evidence)
                self.assertIsNone(verdict.refusal, msg="not refused")
                self.assertIsNone(verdict.warning, msg="no warning")
                self.assertEqual(verdict.facts["result"], "SKIPPED", msg="fact")

    def test_off_still_refuses_a_changed_account(self) -> None:
        """Account consistency is not quota."""
        verdict = judge(QuotaMode.OFF, QuotaEvidence(EvidenceKind.ACCOUNT_CHANGED))
        self.assertEqual(refusal_code(verdict), ProblemCode.ACCOUNT_CHANGED, msg="refused")


class MissingEvidenceTests(unittest.TestCase):
    """Unsupported, unavailable and stale evidence: refused when required, else warned."""

    def test_unsupported_is_refused_when_required(self) -> None:
        """No quota interface and the mode demands one."""
        verdict = judge(QuotaMode.REQUIRED, QuotaEvidence(EvidenceKind.UNSUPPORTED))
        self.assertEqual(refusal_code(verdict), ProblemCode.QUOTA_UNSUPPORTED, msg="refused")

    def test_unsupported_is_admitted_with_a_warning_when_best_effort(self) -> None:
        """Claude Code has no quota interface."""
        verdict = judge(QuotaMode.BEST_EFFORT, QuotaEvidence(EvidenceKind.UNSUPPORTED))
        self.assertIsNone(verdict.refusal, msg="admitted")
        self.assertEqual(warning_code(verdict), ProblemCode.QUOTA_UNSUPPORTED_ALLOWED, msg="warned")
        self.assertEqual(verdict.facts["result"], "UNSUPPORTED_ALLOWED", msg="fact")

    def test_unreadable_evidence_is_unavailable_when_required_and_allowed_when_best_effort(
        self,
    ) -> None:
        """Missing, no snapshot at all."""
        for evidence in (None, QuotaEvidence(EvidenceKind.UNAVAILABLE, detail="timed out")):
            with self.subTest(evidence=evidence):
                required = judge(QuotaMode.REQUIRED, evidence)
                lenient = judge(QuotaMode.BEST_EFFORT, evidence)
                self.assertEqual(refusal_code(required), ProblemCode.QUOTA_UNAVAILABLE, msg="req")
                self.assertIsNone(lenient.refusal, msg="best effort admits")
                self.assertEqual(
                    warning_code(lenient), ProblemCode.QUOTA_UNKNOWN_ALLOWED, msg="warned"
                )

    def test_a_snapshot_at_or_over_the_age_bound_is_stale(self) -> None:
        """The bound is strict: age == max_age is already stale."""
        for age in (MAX_AGE, MAX_AGE + 5):
            with self.subTest(age=age):
                stale = judge(QuotaMode.REQUIRED, snapshot(QuotaWindow(FIVE_HOURS, 1), age_s=age))
                self.assertEqual(refusal_code(stale), ProblemCode.QUOTA_STALE, msg="stale")
        fresh = judge(QuotaMode.REQUIRED, snapshot(QuotaWindow(FIVE_HOURS, 1), age_s=MAX_AGE - 0.1))
        self.assertIsNone(fresh.refusal, msg="just under the bound is fresh")

    def test_a_stale_snapshot_never_overrides_nor_triggers_a_limit(self) -> None:
        """Stale evidence is missing evidence: best effort allows even a 99 percent reading."""
        old = snapshot(QuotaWindow(FIVE_HOURS, 99.0), age_s=MAX_AGE + 1)
        verdict = judge(QuotaMode.BEST_EFFORT, old)
        self.assertIsNone(verdict.refusal, msg="not a valid limit")
        self.assertEqual(warning_code(verdict), ProblemCode.QUOTA_UNKNOWN_ALLOWED, msg="warned")

    def test_a_negative_or_missing_age_is_not_usable(self) -> None:
        """Age must be a nonnegative number below the bound."""
        for age in (-1.0, None, math.nan):
            with self.subTest(age=age):
                verdict = judge(QuotaMode.REQUIRED, snapshot(QuotaWindow(FIVE_HOURS, 1), age_s=age))
                self.assertIsNotNone(verdict.refusal, msg="refused")


class MalformedSnapshotTests(unittest.TestCase):
    """Windows must be consistent and percentages finite and in range."""

    def test_a_snapshot_without_windows_is_unavailable(self) -> None:
        """An empty selected bucket."""
        verdict = judge(QuotaMode.REQUIRED, snapshot())
        self.assertEqual(refusal_code(verdict), ProblemCode.QUOTA_UNAVAILABLE, msg="empty bucket")

    def test_percentages_outside_zero_to_hundred_or_not_finite_are_unavailable(self) -> None:
        """-1, 100.5, infinity and NaN are unreadable, not limits."""
        for bad in (-1.0, 100.5, math.inf, math.nan):
            with self.subTest(percent=bad):
                verdict = judge(QuotaMode.REQUIRED, snapshot(QuotaWindow(FIVE_HOURS, bad)))
                self.assertEqual(refusal_code(verdict), ProblemCode.QUOTA_UNAVAILABLE, msg="bad")

    def test_duplicate_window_durations_are_a_conflict_in_every_mode(self) -> None:
        """Two readings for the same window cannot both be right."""
        twice = snapshot(QuotaWindow(FIVE_HOURS, 10), QuotaWindow(FIVE_HOURS, 20))
        for mode in (QuotaMode.REQUIRED, QuotaMode.BEST_EFFORT):
            with self.subTest(mode=mode):
                self.assertEqual(
                    refusal_code(judge(mode, twice)), ProblemCode.QUOTA_CONFLICT, msg="conflict"
                )

    def test_a_non_positive_window_duration_is_unavailable(self) -> None:
        """A window of zero minutes is not a window."""
        verdict = judge(QuotaMode.REQUIRED, snapshot(QuotaWindow(0, 10)))
        self.assertEqual(refusal_code(verdict), ProblemCode.QUOTA_UNAVAILABLE, msg="bad duration")


class ConflictTests(unittest.TestCase):
    """Conflicting observations refuse in every mode that looks; best effort does not override."""

    def test_conflicting_observations_refuse_in_both_modes(self) -> None:
        """Two identities, or two readings that disagree."""
        for mode in (QuotaMode.REQUIRED, QuotaMode.BEST_EFFORT):
            with self.subTest(mode=mode):
                conflict = judge(mode, QuotaEvidence(EvidenceKind.CONFLICT))
                changed = judge(mode, QuotaEvidence(EvidenceKind.ACCOUNT_CHANGED))
                self.assertEqual(refusal_code(conflict), ProblemCode.QUOTA_CONFLICT, msg="conflict")
                self.assertEqual(refusal_code(changed), ProblemCode.ACCOUNT_CHANGED, msg="changed")


if __name__ == "__main__":
    unittest.main()
