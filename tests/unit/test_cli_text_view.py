"""The text form of a payload: for a person, never a parsing contract.

Owner decision 2026-11-22 (KICKOFF-13): `--format text` shows every member of the object the JSON
form carries, escaped, in the object's own order, under one headline chosen by the object's members.
The expected texts below are literal.
"""

import unittest

from agents_dispatcher.domain.run import TerminalReason
from agents_dispatcher.interfaces.cli.text_view import render_text
from tests.support.cli_book import make_report, make_status

ID = "0b1c2d3e-4f5a-4b6c-8d7e-9f0a1b2c3d4e"


def scalars(value: object) -> list[object]:
    """Return every scalar inside a payload, depth first."""
    if isinstance(value, dict):
        return [item for member in value.values() for item in scalars(member)]
    if isinstance(value, list):
        return [item for member in value for item in scalars(member)]
    return [value]


class HeadlineAndMemberTests(unittest.TestCase):
    """One headline, then the members in their own order."""

    def test_a_started_run(self) -> None:
        """The two members of `start`."""
        self.assertEqual(
            render_text({"run_id": ID, "state": "RUNNING"}),
            f"run {ID} is RUNNING\nrun_id: {ID}\nstate: RUNNING\n",
            msg="started run",
        )

    def test_a_stop_receipt_shows_a_missing_cause_as_a_dash(self) -> None:
        """`None` prints as a dash."""
        self.assertEqual(
            render_text({"run_id": ID, "disposition": "ACCEPTED", "primary_cause": None}),
            f"stop of run {ID}: ACCEPTED\nrun_id: {ID}\ndisposition: ACCEPTED\nprimary_cause: -\n",
            msg="stop receipt",
        )

    def test_a_quarantine_outcome_lists_survivors_as_blocks(self) -> None:
        """A list of objects prints one `- ` block per item; an empty list says so."""
        payload = {
            "run_id": ID,
            "outcome": "STILL_QUARANTINED",
            "survivors": [{"pid": 4242, "start_time": 1.5, "sources": ["lsof", "ps"]}],
            "warnings": [],
        }
        self.assertEqual(
            render_text(payload),
            f"run {ID} outcome STILL_QUARANTINED\n"
            f"run_id: {ID}\n"
            "outcome: STILL_QUARANTINED\n"
            "survivors:\n"
            "  - pid: 4242\n"
            "    start_time: 1.5\n"
            "    sources: lsof, ps\n"
            "warnings: (none)\n",
            msg="quarantine outcome",
        )

    def test_a_pending_answer_nests_the_status(self) -> None:
        """`wait` and `report` of an unfinished run share one shape and one headline."""
        text = render_text({"result": "PENDING", "status": {"revision": 3, "stop": None}})
        self.assertEqual(
            text,
            "result PENDING\nresult: PENDING\nstatus:\n  revision: 3\n  stop: -\n",
            msg="pending answer",
        )

    def test_a_failure_object_flattens_its_code_and_message(self) -> None:
        """The failure object, as every failing command prints it."""
        payload = {"error": {"code": "INVALID_ARGUMENT", "message": "must be above zero"}}
        self.assertEqual(
            render_text(payload),
            "error INVALID_ARGUMENT\ncode: INVALID_ARGUMENT\nmessage: must be above zero\n",
            msg="failure object",
        )

    def test_a_rejection_keeps_its_evaluated_links_beside_the_failure(self) -> None:
        """The failure object with the rejection members nested under their key."""
        payload = {
            "error": {"code": "NO_PROVIDER_ELIGIBLE", "message": "none"},
            "rejection": {"decision": "REJECTED", "context": [{"code": "AUTH_REQUIRED"}]},
        }
        self.assertEqual(
            render_text(payload),
            "error NO_PROVIDER_ELIGIBLE\n"
            "code: NO_PROVIDER_ELIGIBLE\n"
            "message: none\n"
            "rejection:\n"
            "  decision: REJECTED\n"
            "  context:\n"
            "    - code: AUTH_REQUIRED\n",
            msg="rejection beside the failure",
        )

    def test_a_long_or_multi_line_text_is_a_block_and_is_not_cut(self) -> None:
        """The worker's answer is shown whole, every line indented."""
        payload = {"run_id": ID, "terminal_reason": "COMPLETED", "result_text": "one\ntwo"}
        self.assertEqual(
            render_text(payload),
            f"run {ID} ended COMPLETED\n"
            f"run_id: {ID}\n"
            "terminal_reason: COMPLETED\n"
            "result_text:\n"
            "  one\n"
            "  two\n",
            msg="stored report",
        )


class EscapingTests(unittest.TestCase):
    """Nothing from a run can move the cursor."""

    def test_control_characters_in_any_text_are_escaped(self) -> None:
        """A single line and a block alike."""
        text = render_text({"message": "a\x1b[2Jb", "result_text": "x\ry\nz"})
        self.assertNotIn("\x1b", text, msg="no raw ESC")
        self.assertNotIn("\r", text, msg="no raw CR")
        self.assertIn("a\\x1b[2Jb", text, msg="the ESC is shown as an escape")


class EveryMemberTests(unittest.TestCase):
    """Nothing the JSON carries is left out of the text."""

    def test_every_scalar_of_a_status_and_a_report_appears(self) -> None:
        """A renderer that drops a member fails here."""
        for payload in (make_status().as_dict(), make_report(TerminalReason.COMPLETED).as_dict()):
            text = render_text(payload)
            for value in scalars(payload):
                if value is None or isinstance(value, bool):
                    continue
                with self.subTest(value=value):
                    self.assertIn(str(value), text, msg="the member is shown")


if __name__ == "__main__":
    unittest.main()
