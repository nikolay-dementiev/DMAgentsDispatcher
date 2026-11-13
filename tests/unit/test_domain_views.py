"""Tests of the M2 public views: run summary, status, stop receipt, and their bounded text."""

import unittest

from agents_dispatcher.domain.run import RunState, TerminalReason
from agents_dispatcher.domain.views import (
    RunSummary,
    StatusView,
    StopDisposition,
    StopInfo,
    StopReceipt,
    bounded_text,
    validate_reason,
)
from tests.support.run_spies import RUN_ID

SUMMARY_BYTES = 16


class BoundedTextTests(unittest.TestCase):
    """Text shown in a summary never exceeds its byte bound."""

    def test_short_text_is_unchanged(self) -> None:
        """Nothing is added when the text fits."""
        self.assertEqual(bounded_text("add a test", SUMMARY_BYTES), "add a test", msg="fits")

    def test_long_text_is_cut_to_the_bound_with_a_marker(self) -> None:
        """The result, marker included, is within the bound."""
        shown = bounded_text("a" * 100, SUMMARY_BYTES)
        self.assertLessEqual(len(shown.encode("utf-8")), SUMMARY_BYTES, msg="within the bound")
        self.assertTrue(shown.endswith("..."), msg="the cut is visible")

    def test_a_multibyte_character_is_never_split(self) -> None:
        """The cut falls on a character boundary, so the text stays valid."""
        shown = bounded_text("щ" * 100, SUMMARY_BYTES)
        self.assertLessEqual(len(shown.encode("utf-8")), SUMMARY_BYTES, msg="within the bound")
        self.assertEqual(shown.removesuffix("..."), "щ" * (len(shown) - 3), msg="whole characters")


class ReasonTests(unittest.TestCase):
    """The reason of a stop request is bounded in bytes."""

    def test_a_reason_within_the_bound_is_kept(self) -> None:
        """The text is returned as given."""
        self.assertEqual(validate_reason("no longer needed", 512), "no longer needed", msg="kept")

    def test_a_blank_or_oversized_reason_is_refused(self) -> None:
        """Both are invalid input."""
        for reason, limit in (("  ", 512), ("щ" * 300, 512)):
            with self.subTest(length=len(reason)), self.assertRaises(ValueError):
                validate_reason(reason, limit)

    def test_the_bound_counts_bytes_not_characters(self) -> None:
        """Exactly at the bound passes; one byte over fails."""
        self.assertEqual(validate_reason("щ" * 4, 8), "щ" * 4, msg="8 bytes pass at 8")
        with self.assertRaises(ValueError):
            validate_reason("щ" * 4 + "x", 8)


class ViewShapeTests(unittest.TestCase):
    """The JSON subsets of the architecture views (L1658-L1662, L1081)."""

    def summary(self, *, state: RunState = RunState.RUNNING) -> RunSummary:
        """Build a summary."""
        return RunSummary(
            run_id=RUN_ID,
            task="fix the bug",
            state=state,
            reason=TerminalReason.COMPLETED if state is RunState.TERMINAL else None,
            branch=RUN_ID.branch_name if state is RunState.TERMINAL else None,
        )

    def test_run_summary_fields(self) -> None:
        """Exactly the M2 subset, provider included."""
        self.assertEqual(
            self.summary().as_dict(),
            {
                "run_id": RUN_ID.value,
                "task": "fix the bug",
                "provider": "claude-code",
                "state": "RUNNING",
                "reason": None,
                "branch": None,
            },
            msg="summary",
        )

    def test_a_terminal_summary_names_reason_and_branch(self) -> None:
        """Reason and branch appear once the run ended."""
        shown = self.summary(state=RunState.TERMINAL).as_dict()
        self.assertEqual(shown["reason"], "COMPLETED", msg="reason")
        self.assertEqual(shown["branch"], RUN_ID.branch_name, msg="branch")

    def test_status_fields_with_and_without_a_stop_request(self) -> None:
        """Target, revision, run, owner and stop in contract order."""
        plain = StatusView(run=self.summary(), revision=4, owner_alive=True, stop=None).as_dict()
        self.assertEqual(
            list(plain), ["target", "revision", "run", "owner_alive", "stop"], msg="field order"
        )
        self.assertEqual(plain["target"], {"run_id": RUN_ID.value}, msg="target")
        self.assertIsNone(plain["stop"], msg="no stop request")
        asked = StatusView(
            run=self.summary(),
            revision=5,
            owner_alive=True,
            stop=StopInfo(requested_at=12.5, reason="enough"),
        ).as_dict()
        self.assertEqual(asked["stop"], {"requested_at": 12.5, "reason": "enough"}, msg="stop")

    def test_stop_receipt_fields(self) -> None:
        """Run, disposition and the always-null cause."""
        receipt = StopReceipt(run_id=RUN_ID, disposition=StopDisposition.TOO_LATE)
        self.assertEqual(
            receipt.as_dict(),
            {"run_id": RUN_ID.value, "disposition": "TOO_LATE", "primary_cause": None},
            msg="receipt",
        )


if __name__ == "__main__":
    unittest.main()
