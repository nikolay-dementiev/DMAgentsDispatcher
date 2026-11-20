"""Tests of the read-side use cases: status, stop, wait and report."""

import threading
import unittest
from dataclasses import replace

from agents_dispatcher.application.errors import ReportUnavailableError, UnknownRunError
from agents_dispatcher.application.run_queries import ReadReport, RunStatus, StopRun, WaitRun
from agents_dispatcher.domain.run import RunId, RunState, TerminalReason
from agents_dispatcher.domain.views import StopDisposition, StopInfo
from tests.support.clock import FakeClock
from tests.support.run_book import REPORT_JSON, FakeSleeper, RunBook, snapshot
from tests.support.run_spies import RUN_ID

SCAN_S = 1.0
PAUSES = 2
OTHER = RunId("9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d")


def make_status(book: RunBook, summary_bytes: int = 1024) -> RunStatus:
    """Build the status use case over a book."""
    return RunStatus(reader=book, owner=book, summary_bytes=summary_bytes)


class StatusTests(unittest.TestCase):
    """What `status` shows."""

    def test_a_running_run_shows_its_summary_owner_and_revision(self) -> None:
        """Fields come from the snapshot and the owner probe."""
        book = RunBook(runs={RUN_ID: snapshot()}, owner_alive_for={RUN_ID})
        view = make_status(book).status(RUN_ID)
        self.assertEqual(view.run.state, RunState.RUNNING, msg="state")
        self.assertEqual(view.run.task, "fix the bug", msg="task")
        self.assertEqual(view.revision, 1, msg="revision")
        self.assertTrue(view.owner_alive, msg="owner alive")
        self.assertIsNone(view.stop, msg="no stop request")

    def test_a_dead_owner_is_shown_and_nothing_is_closed(self) -> None:
        """`status` only observes."""
        book = RunBook(runs={RUN_ID: snapshot()})
        view = make_status(book).status(RUN_ID)
        self.assertFalse(view.owner_alive, msg="owner not alive")
        self.assertEqual(book.close_calls, [], msg="status never closes a run")

    def test_the_task_is_cut_to_the_summary_bound(self) -> None:
        """A long task is shortened in the view."""
        book = RunBook(runs={RUN_ID: snapshot(task="x" * 100)})
        view = make_status(book, summary_bytes=20).status(RUN_ID)
        self.assertLessEqual(len(view.run.task.encode("utf-8")), 20, msg="within the bound")

    def test_a_recorded_stop_request_is_shown(self) -> None:
        """Time and reason of the request reach the view."""
        asked = replace(snapshot(), stop=StopInfo(requested_at=7.0, reason="enough"))
        view = make_status(RunBook(runs={RUN_ID: asked})).status(RUN_ID)
        self.assertEqual(view.stop, StopInfo(7.0, "enough"), msg="stop info")

    def test_an_unknown_run_is_an_error(self) -> None:
        """The command maps this to exit 21."""
        with self.assertRaises(UnknownRunError):
            make_status(RunBook()).status(RUN_ID)


class StopTests(unittest.TestCase):
    """What `stop` does."""

    def test_the_registry_answer_becomes_the_receipt(self) -> None:
        """Every disposition passes through."""
        for disposition in StopDisposition:
            with self.subTest(disposition=disposition.name):
                book = RunBook(runs={RUN_ID: snapshot()}, stop_answer=disposition)
                receipt = StopRun(stops=book, reason_bytes=512).stop(RUN_ID, "enough")
                self.assertEqual(receipt.disposition, disposition, msg="disposition")
                self.assertEqual(receipt.run_id, RUN_ID, msg="run id")
                self.assertEqual(book.stop_calls, [(RUN_ID, "enough")], msg="reason recorded")

    def test_an_invalid_reason_never_reaches_the_registry(self) -> None:
        """Blank and oversized reasons are refused first."""
        book = RunBook(runs={RUN_ID: snapshot()})
        stop = StopRun(stops=book, reason_bytes=8)
        for reason in ("  ", "x" * 9):
            with self.subTest(reason=reason), self.assertRaises(ValueError):
                stop.stop(RUN_ID, reason)
        self.assertEqual(book.stop_calls, [], msg="the registry was not asked")

    def test_an_unknown_run_is_an_error(self) -> None:
        """No receipt is invented."""
        with self.assertRaises(UnknownRunError):
            StopRun(stops=RunBook(), reason_bytes=512).stop(RUN_ID, "enough")


class EventSleeper:
    """Sleeps for real on an event, so another thread can wake the waiter."""

    def __init__(self, event: threading.Event) -> None:
        """Keep the event."""
        self._event = event

    def sleep(self, seconds: float) -> None:
        """Wait a short real time, or less when the event is set."""
        self._event.wait(min(seconds, 0.05))


class WaitTests(unittest.TestCase):
    """What `wait` does, with an injected clock and sleeper."""

    def rig(self, book: RunBook, scan_s: float = SCAN_S) -> tuple[WaitRun, FakeSleeper, FakeClock]:
        """Build the use case over a book with a fake clock."""
        clock = FakeClock(start=100.0)
        sleeper = FakeSleeper(clock)
        wait = WaitRun(
            status=make_status(book), closer=book, clock=clock, sleeper=sleeper, scan_s=scan_s
        )
        return wait, sleeper, clock

    def test_a_terminal_run_is_ready_at_once(self) -> None:
        """No sleeping, no closing."""
        book = RunBook(runs={RUN_ID: snapshot(RunState.TERMINAL, reason=TerminalReason.COMPLETED)})
        wait, sleeper, _ = self.rig(book)
        result = wait.wait(RUN_ID, timeout_s=10.0)
        self.assertTrue(result.ready, msg="ready")
        self.assertEqual(sleeper.calls, [], msg="did not sleep")
        self.assertEqual(book.close_calls, [], msg="did not try to close a finished run")

    def test_a_run_that_does_not_finish_is_pending_after_the_timeout(self) -> None:
        """The last status is returned; sleeps stay within the deadline."""
        book = RunBook(runs={RUN_ID: snapshot()}, owner_alive_for={RUN_ID})
        wait, sleeper, clock = self.rig(book)
        result = wait.wait(RUN_ID, timeout_s=2.5)
        self.assertFalse(result.ready, msg="pending")
        self.assertEqual(result.status.run.state, RunState.RUNNING, msg="status returned")
        self.assertEqual(sleeper.calls, [1.0, 1.0, 0.5], msg="last sleep is cut to the deadline")
        self.assertEqual(clock.now(), 102.5, msg="stopped at the deadline")

    def test_a_stop_probe_ends_the_wait_between_two_looks(self) -> None:
        """A caller that was interrupted gets the pending status back within one scan."""
        book = RunBook(runs={RUN_ID: snapshot()}, owner_alive_for={RUN_ID})
        wait, sleeper, _ = self.rig(book)
        result = wait.wait(RUN_ID, timeout_s=None, should_stop=lambda: len(sleeper.calls) >= PAUSES)
        self.assertFalse(result.ready, msg="pending, not ready")
        self.assertEqual(len(sleeper.calls), PAUSES, msg="stopped after the second pause")

    def test_a_long_scan_interval_does_not_delay_an_interrupt_by_more_than_a_second(self) -> None:
        """`watchdog.scan_s` of 30 must not make `wait` deaf to Ctrl-C for 30 seconds."""
        book = RunBook(runs={RUN_ID: snapshot()}, owner_alive_for={RUN_ID})
        wait, sleeper, clock = self.rig(book, scan_s=30.0)
        result = wait.wait(RUN_ID, timeout_s=None, should_stop=lambda: bool(sleeper.calls))
        self.assertFalse(result.ready, msg="pending, not ready")
        self.assertLessEqual(max(sleeper.calls), 1.0, msg="no single sleep is longer than a second")
        self.assertLessEqual(clock.now() - 100.0, 1.0, msg="the interrupt was noticed within 1 s")

    def test_a_wait_in_a_worker_thread_ends_when_its_flag_is_set_from_another_thread(self) -> None:
        """A server waits off the main thread, where no signal reaches: only the flag stops it."""
        book = RunBook(runs={RUN_ID: snapshot()}, owner_alive_for={RUN_ID})
        stop = threading.Event()
        wait = WaitRun(
            status=make_status(book),
            closer=book,
            clock=FakeClock(start=100.0),
            sleeper=EventSleeper(stop),
            scan_s=SCAN_S,
        )
        results: list[bool] = []

        def work() -> None:
            results.append(wait.wait(RUN_ID, timeout_s=None, should_stop=stop.is_set).ready)

        worker = threading.Thread(target=work, daemon=True)
        worker.start()
        stop.set()
        worker.join(timeout=10)
        self.assertFalse(worker.is_alive(), msg="the wait ended after the flag was set")
        self.assertEqual(results, [False], msg="and it returned pending")

    def test_a_zero_timeout_looks_once(self) -> None:
        """A poll: no sleeping."""
        book = RunBook(runs={RUN_ID: snapshot()}, owner_alive_for={RUN_ID})
        wait, sleeper, _ = self.rig(book)
        self.assertFalse(wait.wait(RUN_ID, timeout_s=0.0).ready, msg="pending")
        self.assertEqual(sleeper.calls, [], msg="no sleep")

    def test_a_run_that_ends_while_waiting_is_ready(self) -> None:
        """The loop re-reads the store after each sleep."""
        book = RunBook(runs={RUN_ID: snapshot()}, owner_alive_for={RUN_ID})
        wait, sleeper, _ = self.rig(book)
        ended = snapshot(RunState.TERMINAL, reason=TerminalReason.FAILED)

        def finish(call: int) -> None:
            if call == 2:  # noqa: PLR2004 - the second sleep is when the run ends in this test
                book.runs[RUN_ID] = ended

        sleeper.on_sleep = finish
        result = wait.wait(RUN_ID, timeout_s=None)
        self.assertTrue(result.ready, msg="ready")
        self.assertEqual(result.status.run.reason, TerminalReason.FAILED, msg="terminal status")
        self.assertEqual(len(sleeper.calls), 2, msg="stopped waiting once it ended")

    def test_a_dead_owner_is_closed_so_wait_does_not_hang(self) -> None:
        """The closer is asked for this run, and its result is what `wait` reports."""
        orphaned = snapshot(RunState.TERMINAL, reason=TerminalReason.ORPHANED)
        book = RunBook(runs={RUN_ID: snapshot()}, closes_with=orphaned)
        wait, sleeper, _ = self.rig(book)
        result = wait.wait(RUN_ID, timeout_s=None)
        self.assertTrue(result.ready, msg="ready after the close")
        self.assertEqual(result.status.run.reason, TerminalReason.ORPHANED, msg="orphaned")
        self.assertEqual(book.close_calls, [RUN_ID], msg="closed only the target")
        self.assertEqual(sleeper.calls, [], msg="no sleeping on a dead owner")

    def test_an_unknown_run_is_an_error(self) -> None:
        """No waiting for a run that does not exist."""
        wait, sleeper, _ = self.rig(RunBook(runs={OTHER: snapshot()}))
        with self.assertRaises(UnknownRunError):
            wait.wait(RUN_ID, timeout_s=5.0)
        self.assertEqual(sleeper.calls, [], msg="did not sleep")


class ReportTests(unittest.TestCase):
    """What `report` returns."""

    def test_an_unfinished_run_is_pending_with_its_status(self) -> None:
        """No report yet."""
        book = RunBook(runs={RUN_ID: snapshot()})
        result = ReadReport(status=make_status(book)).report(RUN_ID)
        self.assertIsNone(result.report_json, msg="no report")
        self.assertIsNotNone(result.pending, msg="status instead")

    def test_a_finished_run_returns_the_stored_report_text(self) -> None:
        """The text is passed through untouched."""
        done = snapshot(RunState.TERMINAL, reason=TerminalReason.COMPLETED, report_json=REPORT_JSON)
        book = RunBook(runs={RUN_ID: done})
        result = ReadReport(status=make_status(book)).report(RUN_ID)
        self.assertEqual(result.report_json, REPORT_JSON, msg="stored text")
        self.assertIsNone(result.pending, msg="not pending")

    def test_a_finished_run_without_a_stored_report_is_unavailable(self) -> None:
        """A row from an older store has no report; say so instead of inventing one."""
        old = snapshot(RunState.TERMINAL, reason=TerminalReason.COMPLETED)
        book = RunBook(runs={RUN_ID: old})
        with self.assertRaises(ReportUnavailableError):
            ReadReport(status=make_status(book)).report(RUN_ID)

    def test_an_unknown_run_is_an_error(self) -> None:
        """Exit 21 upstream."""
        book = RunBook()
        with self.assertRaises(UnknownRunError):
            ReadReport(status=make_status(book)).report(RUN_ID)


if __name__ == "__main__":
    unittest.main()
