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
    escape_controls,
    stop_disposition,
    validate_reason,
)
from tests.support.run_spies import RUN_ID

SUMMARY_BYTES = 16


class BoundedTextTests(unittest.TestCase):
    """Text shown in a summary never exceeds its byte bound."""

    def test_short_text_is_unchanged(self) -> None:
        """Nothing is added when the text fits."""
        self.assertEqual(bounded_text("add a test", SUMMARY_BYTES), "add a test", msg="fits")

    def test_text_exactly_at_the_bound_is_unchanged(self) -> None:
        """The bound is inclusive."""
        text = "a" * SUMMARY_BYTES
        self.assertEqual(bounded_text(text, SUMMARY_BYTES), text, msg="exactly at the bound")

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


class EscapeControlsTests(unittest.TestCase):
    """Text from a run is printed for a human only after control characters are made visible."""

    def test_control_and_format_characters_become_visible_escapes(self) -> None:
        """ESC, CR, LF, NUL, DEL, C1 and a bidi override are all replaced."""
        self.assertEqual(escape_controls("a\x1b[2Jb"), "a\\x1b[2Jb", msg="ESC")
        self.assertEqual(escape_controls("x\ry\nz\x00"), "x\\x0dy\\x0az\\x00", msg="CR LF NUL")
        self.assertEqual(escape_controls("\x7f\x9b"), "\\x7f\\x9b", msg="DEL and C1")
        self.assertEqual(escape_controls("a\u202eb"), "a\\u202eb", msg="bidi override")

    def test_line_separators_tag_characters_and_lone_surrogates_are_escaped_too(self) -> None:
        """U+2028/2029 break lines, tag characters are invisible, a surrogate cannot be encoded."""
        self.assertEqual(escape_controls("a\u2028b\u2029"), "a\\u2028b\\u2029", msg="separators")
        self.assertEqual(escape_controls("\U000e0041"), "\\U000e0041", msg="tag character")
        self.assertEqual(escape_controls("\ud800"), "\\ud800", msg="lone surrogate")

    def test_ordinary_text_is_unchanged(self) -> None:
        """Letters, spaces, tabs-as-text, accents and emoji pass through."""
        text = "fix the bug: \u00e9 \u4e2d \U0001f600 done"
        self.assertEqual(escape_controls(text), text, msg="nothing to escape")


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


class StopDispositionTests(unittest.TestCase):
    """What a stop request means in each run state (KickOff 5, section 3)."""

    def test_the_disposition_follows_the_state(self) -> None:
        """Early states are effective, FINALIZING is too late, TERMINAL stays terminal."""
        expected = {
            RunState.ACCEPTED: StopDisposition.EFFECTIVE,
            RunState.STARTING: StopDisposition.EFFECTIVE,
            RunState.RUNNING: StopDisposition.EFFECTIVE,
            RunState.STOPPING: StopDisposition.ALREADY_STOPPING,
            RunState.FINALIZING: StopDisposition.TOO_LATE,
            RunState.TERMINAL: StopDisposition.TERMINAL,
        }
        for state, disposition in expected.items():
            with self.subTest(state=state.name):
                self.assertEqual(
                    stop_disposition(state, already_requested=False),
                    disposition,
                    msg=f"first request in {state.name}",
                )

    def test_a_second_request_is_already_stopping_unless_the_run_moved_on(self) -> None:
        """A request that exists wins over the live states but not over FINALIZING or the end."""
        for state in (RunState.ACCEPTED, RunState.STARTING, RunState.RUNNING, RunState.STOPPING):
            with self.subTest(state=state.name):
                self.assertEqual(
                    stop_disposition(state, already_requested=True),
                    StopDisposition.ALREADY_STOPPING,
                    msg=f"repeat in {state.name}",
                )
        self.assertEqual(
            stop_disposition(RunState.FINALIZING, already_requested=True),
            StopDisposition.TOO_LATE,
            msg="too late wins in FINALIZING",
        )
        self.assertEqual(
            stop_disposition(RunState.TERMINAL, already_requested=True),
            StopDisposition.TERMINAL,
            msg="terminal wins after the end",
        )


if __name__ == "__main__":
    unittest.main()
