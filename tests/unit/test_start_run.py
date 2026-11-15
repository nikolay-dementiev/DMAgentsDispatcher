"""Tests of `start`: accept the run, launch the supervisor, wait for its acknowledgement."""

import json
import unittest
from dataclasses import dataclass

from agents_dispatcher.adapters.system.json_report import JsonReportRenderer
from agents_dispatcher.application.errors import (
    DecisionLogError,
    InputTooLargeError,
    InvalidRouteRequestError,
    LaunchError,
    LaunchLostError,
    NoProviderEligibleError,
    StartInterruptedError,
    UnknownTaskTypeError,
    WorkspaceNotGitError,
    WriteTaskNotAllowedError,
)
from agents_dispatcher.application.ports import FinishRecord
from agents_dispatcher.application.start_run import StartRun
from agents_dispatcher.domain.routing import (
    Recommendation,
    Rejected,
    RouteMode,
    RunRouting,
    TaskMode,
)
from agents_dispatcher.domain.run import Limits, RunId, RunState, TaskSpec, TerminalReason
from agents_dispatcher.domain.views import RunSummary
from tests.support.clock import FakeClock
from tests.support.routing import REFUSED, STARTED, TASK, FixedResolver, FixedRouter, SpyDecisions
from tests.support.run_book import FakeSleeper
from tests.support.run_spies import RUN_ID, Journal, SpyIds
from tests.support.start_book import SpyLauncher, SpyRepository, StartBook

SPEC = TaskSpec(repo="/work/example", task="fix the bug", hard_s=60.0)
DEFAULTS = Limits(hard_s=3600.0, memory_bytes=2048)
STARTUP_S = 30.0
SCAN_S = 1.0


class SpySweeper:
    """Records that stale runs were swept."""

    def __init__(self, journal: Journal) -> None:
        """Keep the journal."""
        self._journal = journal

    def sweep(self) -> list[RunId]:
        """Record the sweep; nothing was closed."""
        self._journal.add("sweep")
        return []


@dataclass
class Rig:
    """A `StartRun` wired to fakes, plus the fakes."""

    start: StartRun
    journal: Journal
    book: StartBook
    launcher: SpyLauncher
    repository: SpyRepository
    clock: FakeClock
    sleeper: FakeSleeper
    resolver: FixedResolver
    router: FixedRouter
    decisions: SpyDecisions


def make_rig(
    *,
    claim_at_sleep: int | None = None,
    fault_at_sleep: int | None = None,
    interrupt_at_sleep: int | None = None,
    startup_s: float = STARTUP_S,
    input_bytes: int = 1024,
) -> Rig:
    """Build the use case; the fake supervisor claims the run at the given sleep, or never."""
    journal = Journal()
    clock = FakeClock(start=100.0)
    book = StartBook(journal)
    launcher = SpyLauncher(journal, book)
    repository = SpyRepository(journal)

    def on_sleep(call: int) -> None:
        if claim_at_sleep == call:
            book.claim_run(RUN_ID)
        if interrupt_at_sleep == call:
            raise KeyboardInterrupt
        if fault_at_sleep == call:
            book.faults[RUN_ID] = "OSError: disk is full"

    sleeper = FakeSleeper(clock, on_sleep)
    resolver = FixedResolver()
    router = FixedRouter(journal)
    decisions = SpyDecisions(journal)
    start = StartRun(
        repository=repository,
        ids=SpyIds(journal),
        store=book,
        sweeper=SpySweeper(journal),
        launcher=launcher,
        clock=clock,
        sleeper=sleeper,
        renderer=JsonReportRenderer(),
        default_limits=DEFAULTS,
        startup_s=startup_s,
        scan_s=SCAN_S,
        summary_bytes=1024,
        input_bytes=input_bytes,
        resolver=resolver,
        router=router,
        decisions=decisions,
    )
    return Rig(
        start, journal, book, launcher, repository, clock, sleeper, resolver, router, decisions
    )


class StartTests(unittest.TestCase):
    """The happy path and the order of the steps."""

    def test_the_run_is_accepted_before_the_supervisor_is_launched(self) -> None:
        """The row exists first, so a supervisor that is fast always finds its run."""
        rig = make_rig(claim_at_sleep=1)
        rig.start.start(SPEC)
        self.assertEqual(
            rig.journal.events[:7],
            [
                "repo.check",
                "route",
                "decision.log",
                "sweep",
                "ids.new",
                "store.accept",
                "launcher.launch",
            ],
            msg="check, route, log the decision, sweep of abandoned runs, id, accept, launch",
        )

    def test_the_accepted_run_carries_the_task_and_the_effective_limits(self) -> None:
        """The spec's override wins, the configured default fills the rest."""
        rig = make_rig(claim_at_sleep=1)
        rig.start.start(SPEC)
        accepted = rig.book.accepted[0]
        self.assertEqual(accepted.limits, Limits(60.0, 2048), msg="override and default")
        self.assertEqual((accepted.repo, accepted.task), ("/work/example", "fix the bug"), msg="id")

    def test_the_brief_goes_to_the_launcher_and_the_summary_shows_the_started_run(self) -> None:
        """The launcher gets the full task; the caller gets the run in state STARTING."""
        rig = make_rig(claim_at_sleep=1)
        summary = rig.start.start(SPEC)
        assert isinstance(summary, RunSummary)  # noqa: S101 - narrows the outcome for the checker
        self.assertEqual(rig.launcher.launched, [(RUN_ID, "fix the bug")], msg="brief")
        self.assertEqual(summary.run_id, RUN_ID, msg="run id")
        self.assertEqual(summary.state, RunState.STARTING, msg="acknowledged")

    def test_an_instant_acknowledgement_needs_no_sleep(self) -> None:
        """A supervisor that claimed during the launch is seen on the first look."""
        rig = make_rig()
        rig.launcher.claims_at_once = True
        rig.start.start(SPEC)
        self.assertEqual(rig.sleeper.calls, [], msg="no waiting")

    def test_a_non_git_repository_is_refused_before_anything_is_stored(self) -> None:
        """Exit 2 material: nothing accepted, nothing launched."""
        rig = make_rig()
        rig.repository.not_git = True
        with self.assertRaises(WorkspaceNotGitError):
            rig.start.start(SPEC)
        self.assertEqual(rig.journal.events, ["repo.check"], msg="nothing else was touched")


class RoutingTests(unittest.TestCase):
    """The task type and the provider restriction decide whether and how a run starts."""

    def test_the_accepted_run_keeps_the_decision_that_started_it(self) -> None:
        """Task type, mode, the selected provider with model and effort, and the route."""
        rig = make_rig(claim_at_sleep=1)
        rig.start.start(TaskSpec(repo="/work/example", task="x", task_type="implement"))
        self.assertEqual(rig.resolver.asked, ["implement"], msg="the task type was resolved")
        self.assertEqual(
            rig.book.accepted[0].routing,
            RunRouting.from_started(TASK, STARTED),
            msg="the stored routing is the decision",
        )

    def test_a_provider_restriction_is_a_start_request_and_none_is_a_route_request(self) -> None:
        """`--provider` evaluates one link only; the log says which kind of request it was."""
        restricted = make_rig(claim_at_sleep=1)
        restricted.start.start(TaskSpec(repo="/work/example", task="x", provider="anthropic"))
        open_chain = make_rig(claim_at_sleep=1)
        open_chain.start.start(SPEC)
        self.assertEqual(restricted.router.only, ["anthropic"], msg="the restriction reached it")
        self.assertEqual(open_chain.router.only, [None], msg="no restriction")
        self.assertEqual(
            [restricted.decisions.recorded[0][2], open_chain.decisions.recorded[0][2]],
            [RouteMode.START, RouteMode.ROUTE],
            msg="mode of each request",
        )

    def test_a_recommendation_creates_nothing_and_is_returned(self) -> None:
        """No sweep, no run row, no supervisor; the decision is still logged."""
        rig = make_rig()
        recommendation = Recommendation(
            (REFUSED,), model="m", effort="e", mode=TaskMode.READ, task_type=None
        )
        rig.router.decision = recommendation
        outcome = rig.start.start(SPEC)
        self.assertIs(outcome, recommendation, msg="the advice is the answer")
        self.assertEqual(rig.journal.events, ["repo.check", "route", "decision.log"], msg="only")
        self.assertEqual(rig.book.accepted, [], msg="no run row")

    def test_a_rejection_raises_with_the_route_and_creates_nothing(self) -> None:
        """Exit 20 material: the refusals travel with the error."""
        rig = make_rig()
        rejected = Rejected((REFUSED,), task_type=None)
        rig.router.decision = rejected
        with self.assertRaises(NoProviderEligibleError) as caught:
            rig.start.start(SPEC)
        self.assertIs(caught.exception.rejected, rejected, msg="the refusals are kept")
        self.assertEqual(rig.journal.events, ["repo.check", "route", "decision.log"], msg="only")
        self.assertEqual(rig.book.accepted, [], msg="no run row")

    def test_a_task_that_cannot_be_resolved_touches_nothing(self) -> None:
        """An unknown or forbidden task type is refused before the repository is looked at."""
        for error in (UnknownTaskTypeError("unknown"), WriteTaskNotAllowedError("forbidden")):
            with self.subTest(error=type(error).__name__):
                rig = make_rig()
                rig.resolver.error = error
                with self.assertRaises(type(error)):
                    rig.start.start(SPEC)
                self.assertEqual(rig.journal.events, [], msg="nothing was checked or stored")

    def test_a_request_the_chain_cannot_have_touches_nothing_but_the_router(self) -> None:
        """`--provider` naming a provider outside the chain is a request error."""
        rig = make_rig()
        rig.router.error = InvalidRouteRequestError("not in the chain")
        with self.assertRaises(InvalidRouteRequestError):
            rig.start.start(SPEC)
        self.assertEqual(rig.book.accepted, [], msg="no run row")
        self.assertEqual(rig.decisions.recorded, [], msg="no decision was reached")

    def test_a_decision_that_cannot_be_logged_creates_nothing(self) -> None:
        """The log line comes first; a failed write fails the command."""
        rig = make_rig()
        rig.decisions.error = DecisionLogError("sdk.log: disk full")
        with self.assertRaises(DecisionLogError):
            rig.start.start(SPEC)
        self.assertEqual(rig.book.accepted, [], msg="no run row")
        self.assertEqual(rig.launcher.launched, [], msg="no supervisor")


class InputBoundTests(unittest.TestCase):
    """The task text is bounded in UTF-8 bytes before anything is created."""

    def test_a_task_over_the_bound_is_refused_and_nothing_is_touched(self) -> None:
        """Exit 2 material: not even the repository is looked at."""
        rig = make_rig(input_bytes=10)
        with self.assertRaises(InputTooLargeError) as caught:
            rig.start.start(TaskSpec(repo="/work/example", task="x" * 11))
        self.assertIn("10", str(caught.exception), msg="the bound is named")
        self.assertEqual(rig.journal.events, [], msg="nothing was checked, stored or launched")

    def test_the_bound_counts_utf8_bytes_not_characters(self) -> None:
        """Five two-byte characters are ten bytes: allowed at 10, refused at 9."""
        task = "\u00e9" * 5
        make_rig(claim_at_sleep=1, input_bytes=10).start.start(
            TaskSpec(repo="/work/example", task=task)
        )
        with self.assertRaises(InputTooLargeError):
            make_rig(input_bytes=9).start.start(TaskSpec(repo="/work/example", task=task))


class InterruptedStartTests(unittest.TestCase):
    """Ctrl-C while the supervisor is taking over: the accepted run is named, not abandoned."""

    def test_an_interrupt_after_the_run_was_accepted_names_the_run_and_leaves_it_alone(
        self,
    ) -> None:
        """The supervisor may still claim the run; closing it here would race with that."""
        rig = make_rig(interrupt_at_sleep=2)
        with self.assertRaises(StartInterruptedError) as caught:
            rig.start.start(SPEC)
        self.assertEqual(caught.exception.run_id, RUN_ID, msg="the run id is named")
        self.assertEqual(rig.book.reasons, {}, msg="the run was not closed")

    def test_an_interrupt_before_anything_is_accepted_stays_a_plain_interrupt(self) -> None:
        """No run exists, so there is nothing to name."""
        rig = make_rig()
        rig.repository.error = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            rig.start.start(SPEC)


class LaunchLostTests(unittest.TestCase):
    """A supervisor that never takes over is a LAUNCH_LOST run, not a hung command."""

    def test_no_acknowledgement_within_the_bound_ends_the_run_as_launch_lost(self) -> None:
        """The run is closed with a stored report and the caller gets the run id."""
        rig = make_rig()
        with self.assertRaises(LaunchLostError) as caught:
            rig.start.start(SPEC)
        self.assertEqual(caught.exception.run_id, RUN_ID, msg="the id is reported")
        self.assertEqual(rig.book.reasons[RUN_ID], TerminalReason.LAUNCH_LOST, msg="closed")
        report = json.loads(rig.book.closed[0][1].report_json)
        self.assertEqual(report["terminal_reason"], "LAUNCH_LOST", msg="report reason")
        self.assertIn("30", " ".join(report["notes"]), msg="the note names the bound")
        self.assertEqual(report["provider"], "anthropic", msg="the lost run keeps its routing")

    def test_the_wait_is_bounded_by_the_startup_setting(self) -> None:
        """Polling at the scan interval, never past `watchdog.startup_s`."""
        rig = make_rig()
        with self.assertRaises(LaunchLostError):
            rig.start.start(SPEC)
        self.assertEqual(sum(rig.sleeper.calls), STARTUP_S, msg="slept exactly the bound")
        self.assertLessEqual(max(rig.sleeper.calls), SCAN_S, msg="at the scan interval")

    def test_the_last_sleep_never_overshoots_the_bound(self) -> None:
        """A bound that is not a multiple of the scan interval ends exactly on time."""
        rig = make_rig(startup_s=2.5)
        with self.assertRaises(LaunchLostError):
            rig.start.start(SPEC)
        self.assertEqual(rig.sleeper.calls, [1.0, 1.0, 0.5], msg="the last step is shortened")

    def test_a_fault_the_supervisor_recorded_ends_the_wait_and_is_named(self) -> None:
        """No waiting out the bound for a supervisor that already said why it gave up."""
        rig = make_rig(fault_at_sleep=2)
        with self.assertRaises(LaunchLostError) as caught:
            rig.start.start(SPEC)
        self.assertEqual(len(rig.sleeper.calls), 2, msg="stopped at the fault, not at the bound")
        self.assertIn("OSError: disk is full", str(caught.exception), msg="the cause is named")
        report = json.loads(rig.book.closed[0][1].report_json)
        self.assertIn("OSError: disk is full", " ".join(report["notes"]), msg="and stored")

    def test_a_launcher_failure_ends_the_run_at_once(self) -> None:
        """No waiting for a supervisor that could not even be started."""
        rig = make_rig()
        rig.launcher.error = LaunchError("cannot start the supervisor: no such file")
        with self.assertRaises(LaunchLostError) as caught:
            rig.start.start(SPEC)
        self.assertEqual(rig.sleeper.calls, [], msg="no wait")
        self.assertIn("no such file", str(caught.exception), msg="the cause is kept")
        self.assertEqual(rig.book.reasons[RUN_ID], TerminalReason.LAUNCH_LOST, msg="closed")

    def test_a_supervisor_that_claims_at_the_last_moment_wins(self) -> None:
        """If the close finds the run already claimed, the run started after all."""
        rig = make_rig()
        original = rig.book.finish_unclaimed

        def claim_then_refuse(run_id: RunId, record: FinishRecord) -> bool:
            rig.book.claim_run(run_id)
            return original(run_id, record)

        rig.book.finish_unclaimed = claim_then_refuse  # type: ignore[method-assign]
        summary = rig.start.start(SPEC)
        assert isinstance(summary, RunSummary)  # noqa: S101 - narrows the outcome for the checker
        self.assertEqual(summary.state, RunState.STARTING, msg="the run is running")


if __name__ == "__main__":
    unittest.main()
