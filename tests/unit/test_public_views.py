"""Tests of the public payloads: the one source of what a front end shows for each operation."""

import json
import unittest

from agents_dispatcher.application.public_views import (
    report_payload,
    started_payload,
    stop_payload,
    wait_payload,
)

from agents_dispatcher.application.run_queries import ReportResult
from agents_dispatcher.domain.run import RunState, TerminalReason
from agents_dispatcher.domain.views import StopDisposition, StopReceipt
from tests.support.cli_book import (
    PENDING,
    ended,
    make_report,
    make_status,
    make_summary,
    report_text,
)
from tests.support.run_spies import RUN_ID


class PayloadTests(unittest.TestCase):
    """Each payload is the dict the command line printed before the tools existed."""

    def test_a_started_run_shows_its_id_and_state_name(self) -> None:
        """`start` and `route` answer with the id and the state, nothing else."""
        payload = started_payload(make_summary())
        self.assertEqual(
            payload, {"run_id": RUN_ID.value, "state": "STARTING"}, msg="id and state name"
        )

    def test_a_wait_shows_ready_or_pending_with_the_status(self) -> None:
        """The result word and the status view of the run."""
        done = ended(TerminalReason.COMPLETED)
        self.assertEqual(
            wait_payload(done),
            {"result": "READY", "status": done.status.as_dict()},
            msg="a finished run is READY",
        )
        self.assertEqual(
            wait_payload(PENDING),
            {"result": "PENDING", "status": PENDING.status.as_dict()},
            msg="a run still going is PENDING",
        )

    def test_a_stop_shows_the_receipt(self) -> None:
        """The receipt dict of the stop request."""
        receipt = StopReceipt(RUN_ID, StopDisposition.EFFECTIVE)
        self.assertEqual(stop_payload(receipt), receipt.as_dict(), msg="the receipt as is")

    def test_a_report_is_the_stored_object_or_the_pending_status(self) -> None:
        """A finished run gives its stored report; an unfinished one gives PENDING."""
        stored = report_text(make_report(TerminalReason.COMPLETED))
        self.assertEqual(
            report_payload(ReportResult(stored, None)), json.loads(stored), msg="stored report"
        )
        pending = make_status(RunState.RUNNING)
        self.assertEqual(
            report_payload(ReportResult(None, pending)),
            {"result": "PENDING", "status": pending.as_dict()},
            msg="unfinished run",
        )


if __name__ == "__main__":
    unittest.main()
