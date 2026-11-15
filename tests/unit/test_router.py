"""Tests of the router: a chain of responsibility over provider links, behind a plugin port."""

import unittest
from collections.abc import Mapping
from pathlib import Path

import agents_dispatcher
from agents_dispatcher.application.errors import InvalidRouteRequestError
from agents_dispatcher.application.routing import GateResult, ProviderPlugin, Router
from agents_dispatcher.application.settings import ProviderSettings
from agents_dispatcher.domain.routing import (
    Chain,
    Disposition,
    HandoffLink,
    Problem,
    ProblemCode,
    ProviderLink,
    Recommendation,
    Rejected,
    ResolvedTask,
    Started,
    TaskMode,
)

CODEX = "builtin.codex"
CLAUDE = "builtin.claude"
FAKE = "test.fake"


class SpyPlugin:
    """A provider implementation that records the tasks it was asked about."""

    def __init__(self, *problems: Problem) -> None:
        """Refuse every task with `problems`; accept it when there are none."""
        self._problems = problems
        self.asked: list[ResolvedTask] = []

    def check(self, task: ResolvedTask) -> tuple[Problem, ...]:
        """Record the question and answer with the configured problems."""
        self.asked.append(task)
        return self._problems


def provider(
    implementation: str, model: str, *, enabled: bool = True, effort: str = "medium"
) -> ProviderSettings:
    """Build the settings of one provider."""
    return ProviderSettings(
        enabled=enabled,
        implementation=implementation,
        model=model,
        effort=effort,
        capacity=5,
    )


def default_providers(*, anthropic_enabled: bool = True) -> dict[str, ProviderSettings]:
    """Return the shipped pair: openai (not loaded in this version) and anthropic."""
    return {
        "openai": provider(CODEX, "gpt-6.1-sol"),
        "anthropic": provider(CLAUDE, "claude-sonnet-5-5", enabled=anthropic_enabled),
    }


def default_task(*links: ProviderLink | HandoffLink) -> ResolvedTask:
    """Build a read task over the architecture default chain, or over `links`."""
    chain = links or (ProviderLink("openai"), ProviderLink("anthropic"), HandoffLink())
    return ResolvedTask("audit", TaskMode.READ, Chain(chain))


class FakeGate:
    """An account gate with a canned answer that records who it was asked about."""

    def __init__(self, result: GateResult) -> None:
        """Keep the answer."""
        self.result = result
        self.asked: list[str] = []

    def assess(self, provider: str, task: ResolvedTask) -> GateResult:  # noqa: ARG002
        """Record the question and answer."""
        self.asked.append(provider)
        return self.result


class FakeOccupancy:
    """Counts of non-terminal runs per provider, as the store would report them."""

    def __init__(self, **counts: int) -> None:
        """Keep the counts by provider id."""
        self._counts = counts

    def active(self, provider: str) -> int:
        """Return how many non-terminal runs `provider` holds."""
        return self._counts.get(provider, 0)


def make_router(
    plugins: Mapping[str, ProviderPlugin],
    providers: Mapping[str, ProviderSettings],
    occupancy: FakeOccupancy | None = None,
    gates: Mapping[str, FakeGate] | None = None,
) -> Router:
    """Compose a router the way a composition root does."""
    return Router(
        plugins,
        providers,
        occupancy=occupancy or FakeOccupancy(),
        gates=gates or {},
        recommendation_model="gpt-6.1-sol",
        recommendation_effort="medium",
    )


class WalkTests(unittest.TestCase):
    """The walk stops at the first link that takes the task; only refusals advance."""

    def test_the_default_chain_refuses_openai_and_selects_anthropic_in_order(self) -> None:
        """PROVIDER_NOT_LOADED, then the selection, in chain order."""
        decision = make_router({CLAUDE: SpyPlugin()}, default_providers()).route(default_task())
        self.assertIsInstance(decision, Started, msg="a run starts")
        assert isinstance(decision, Started)  # noqa: S101 - narrows the type for the checker
        self.assertEqual(
            [(e.provider, e.disposition) for e in decision.evaluations],
            [("openai", Disposition.REFUSED), ("anthropic", Disposition.SELECTED)],
            msg="every evaluated link, in chain order",
        )
        self.assertEqual(
            [p.code for p in decision.evaluations[0].problems],
            [ProblemCode.PROVIDER_NOT_LOADED],
            msg="openai is not loaded",
        )
        self.assertEqual(
            (decision.provider, decision.model, decision.effort),
            ("anthropic", "claude-sonnet-5-5", "medium"),
            msg="the provider's own model and effort",
        )

    def test_a_link_model_and_effort_override_the_provider_values(self) -> None:
        """Link attributes win for this task only."""
        task = default_task(ProviderLink("anthropic", model="opus", effort="high"))
        decision = make_router({CLAUDE: SpyPlugin()}, default_providers()).route(task)
        assert isinstance(decision, Started)  # noqa: S101 - narrows the type for the checker
        self.assertEqual((decision.model, decision.effort), ("opus", "high"), msg="link wins")

    def test_the_walk_stops_at_the_first_selected_link(self) -> None:
        """Later links are never asked."""
        first, second = SpyPlugin(), SpyPlugin()
        providers = {"one": provider(FAKE, "m1"), "two": provider("test.other", "m2")}
        task = default_task(ProviderLink("one"), ProviderLink("two"))
        decision = make_router({FAKE: first, "test.other": second}, providers).route(task)
        assert isinstance(decision, Started)  # noqa: S101 - narrows the type for the checker
        self.assertEqual(decision.provider, "one", msg="the first eligible link")
        self.assertEqual(second.asked, [], msg="the second link was never asked")
        self.assertEqual(len(decision.evaluations), 1, msg="only the evaluated links are listed")

    def test_a_plugin_refusal_advances_to_the_next_link(self) -> None:
        """EXECUTABLE_UNAVAILABLE comes from the plugin, not from the router."""
        missing = Problem(ProblemCode.EXECUTABLE_UNAVAILABLE, "no such tool")
        providers = {"one": provider(FAKE, "m1"), "two": provider("test.other", "m2")}
        plugins = {FAKE: SpyPlugin(missing), "test.other": SpyPlugin()}
        task = default_task(ProviderLink("one"), ProviderLink("two"))
        decision = make_router(plugins, providers).route(task)
        assert isinstance(decision, Started)  # noqa: S101 - narrows the type for the checker
        self.assertEqual(decision.evaluations[0].problems, (missing,), msg="the plugin's problem")
        self.assertEqual(decision.provider, "two", msg="the next link took the task")

    def test_a_disabled_provider_is_never_asked_and_lists_both_problems(self) -> None:
        """Unloaded and disabled: both listed in order; a disabled provider is not probed."""
        spy = SpyPlugin()
        providers = {"x": provider(CODEX, "m", enabled=False)}
        decision = make_router({FAKE: spy}, providers).route(default_task(ProviderLink("x")))
        assert isinstance(decision, Rejected)  # noqa: S101 - narrows the type for the checker
        self.assertEqual(
            [p.code for p in decision.context[0].problems],
            [ProblemCode.PROVIDER_NOT_LOADED, ProblemCode.PROVIDER_DISABLED],
            msg="both problems, in order",
        )
        loaded = {"x": provider(FAKE, "m", enabled=False)}
        decision = make_router({FAKE: spy}, loaded).route(default_task(ProviderLink("x")))
        self.assertEqual(spy.asked, [], msg="a disabled provider is not probed")


class OutcomeTests(unittest.TestCase):
    """Recommendation, rejection and the START mode."""

    def test_all_refused_with_a_handoff_is_a_labelled_recommendation(self) -> None:
        """Q-C: refusal reasons, the recommended model and effort, the mode."""
        router = make_router({CLAUDE: SpyPlugin()}, default_providers(anthropic_enabled=False))
        decision = router.route(default_task())
        assert isinstance(decision, Recommendation)  # noqa: S101 - narrows the type
        self.assertEqual(
            [[p.code for p in e.problems] for e in decision.refusals],
            [[ProblemCode.PROVIDER_NOT_LOADED], [ProblemCode.PROVIDER_DISABLED]],
            msg="both refusals, in order",
        )
        self.assertEqual(
            (decision.model, decision.effort, decision.mode, decision.task_type),
            ("gpt-6.1-sol", "medium", TaskMode.READ, "audit"),
            msg="what is recommended, and for which task",
        )

    def test_all_refused_without_a_handoff_is_a_rejection(self) -> None:
        """NO_PROVIDER_ELIGIBLE with the whole context."""
        router = make_router({CLAUDE: SpyPlugin()}, default_providers(anthropic_enabled=False))
        decision = router.route(default_task(ProviderLink("openai"), ProviderLink("anthropic")))
        assert isinstance(decision, Rejected)  # noqa: S101 - narrows the type for the checker
        self.assertEqual(len(decision.context), 2, msg="both links are in the context")

    def test_start_mode_evaluates_only_the_named_link(self) -> None:
        """No alternates: the other plugin is never asked."""
        other = SpyPlugin()
        providers = {"one": provider(FAKE, "m1"), "two": provider("test.other", "m2")}
        task = default_task(ProviderLink("one"), ProviderLink("two"))
        decision = make_router({FAKE: SpyPlugin(), "test.other": other}, providers).route(
            task, only="two"
        )
        assert isinstance(decision, Started)  # noqa: S101 - narrows the type for the checker
        self.assertEqual([e.provider for e in decision.evaluations], ["two"], msg="one link")
        self.assertEqual(len(other.asked), 1, msg="the named plugin was asked")

    def test_start_mode_never_hands_off_even_when_the_chain_ends_with_a_handoff(self) -> None:
        """A refused named link is a rejection."""
        router = make_router({CLAUDE: SpyPlugin()}, default_providers(anthropic_enabled=False))
        decision = router.route(default_task(), only="anthropic")
        assert isinstance(decision, Rejected)  # noqa: S101 - narrows the type for the checker
        self.assertEqual([e.provider for e in decision.context], ["anthropic"], msg="one link")

    def test_a_named_provider_outside_the_chain_is_a_request_error(self) -> None:
        """Not an eligibility problem: the caller asked for something the task does not have."""
        router = make_router({CLAUDE: SpyPlugin()}, default_providers())
        task = default_task(ProviderLink("anthropic"))
        with self.assertRaises(InvalidRouteRequestError, msg="not in the chain") as caught:
            router.route(task, only="openai")
        self.assertIn("openai", str(caught.exception), msg="names the provider")


class CapacityTests(unittest.TestCase):
    """A provider holds at most `capacity` non-terminal runs; the route shows who was full."""

    def route(
        self, occupancy: FakeOccupancy, contended: frozenset[str] = frozenset()
    ) -> Started | Recommendation | Rejected:
        """Route the default chain with both providers loaded."""
        router = make_router(
            {CODEX: SpyPlugin(), CLAUDE: SpyPlugin()}, default_providers(), occupancy
        )
        return router.route(default_task(), contended=contended)

    def test_a_provider_below_capacity_is_selected(self) -> None:
        """Four of five slots taken still leaves one."""
        decision = self.route(FakeOccupancy(openai=4))
        assert isinstance(decision, Started)  # noqa: S101 - narrows the type for the checker
        self.assertEqual(decision.provider, "openai", msg="first link takes it")

    def test_a_provider_at_capacity_is_refused_and_the_chain_moves_on(self) -> None:
        """Five of five: CAPACITY_CONTENDED, then the next link answers."""
        decision = self.route(FakeOccupancy(openai=5))
        assert isinstance(decision, Started)  # noqa: S101 - narrows the type for the checker
        self.assertEqual(decision.provider, "anthropic", msg="next link takes it")
        (problem,) = decision.evaluations[0].problems
        self.assertEqual(problem.code, ProblemCode.CAPACITY_CONTENDED, msg="the refusal code")
        self.assertIn("openai", problem.message, msg="names the provider")

    def test_each_provider_counts_only_its_own_runs(self) -> None:
        """A full anthropic does not block openai."""
        decision = self.route(FakeOccupancy(anthropic=5))
        assert isinstance(decision, Started)  # noqa: S101 - narrows the type for the checker
        self.assertEqual(decision.provider, "openai", msg="openai still free")

    def test_a_provider_named_as_contended_is_refused_whatever_the_count(self) -> None:
        """The store said no a moment ago; the re-route must not pick it again."""
        decision = self.route(FakeOccupancy(), contended=frozenset({"openai"}))
        assert isinstance(decision, Started)  # noqa: S101 - narrows the type for the checker
        self.assertEqual(decision.provider, "anthropic", msg="moved on")
        codes = [p.code for p in decision.evaluations[0].problems]
        self.assertEqual(codes, [ProblemCode.CAPACITY_CONTENDED], msg="shown in the route")

    def test_the_capacity_comes_from_the_provider_settings(self) -> None:
        """A capacity of 1 is full after one run."""
        providers = default_providers()
        providers["openai"] = ProviderSettings(
            enabled=True, implementation=CODEX, model="gpt-6.1-sol", effort="medium", capacity=1
        )
        router = make_router(
            {CODEX: SpyPlugin(), CLAUDE: SpyPlugin()}, providers, FakeOccupancy(openai=1)
        )
        decision = router.route(default_task())
        assert isinstance(decision, Started)  # noqa: S101 - narrows the type for the checker
        self.assertEqual(decision.provider, "anthropic", msg="capacity 1 reached")


class GateTests(unittest.TestCase):
    """The account gate of an implementation adds login and quota refusals, warnings, evidence."""

    OK = GateResult(evidence={"login": {"state": "LOGGED_IN"}})

    def route(
        self, gate: FakeGate, *, occupancy: FakeOccupancy | None = None, enabled: bool = True
    ) -> Started | Recommendation | Rejected:
        """Route the default chain; only the codex implementation has a gate."""
        providers = default_providers()
        providers["openai"] = provider(CODEX, "gpt-6.1-sol", enabled=enabled)
        router = make_router(
            {CODEX: SpyPlugin(), CLAUDE: SpyPlugin()}, providers, occupancy, {CODEX: gate}
        )
        return router.route(default_task())

    def test_a_gate_that_finds_nothing_wrong_selects_and_its_evidence_is_kept(self) -> None:
        """The selected link shows what the gate read."""
        gate = FakeGate(self.OK)
        decision = self.route(gate)
        assert isinstance(decision, Started)  # noqa: S101 - narrows the type for the checker
        self.assertEqual(decision.provider, "openai", msg="selected")
        self.assertEqual(
            decision.selected.evidence, {"login": {"state": "LOGGED_IN"}}, msg="evidence kept"
        )

    def test_gate_warnings_ride_on_the_selected_link(self) -> None:
        """Admitted with a warning is still admitted."""
        warning = Problem(ProblemCode.QUOTA_UNKNOWN_ALLOWED, "quota unreadable")
        decision = self.route(FakeGate(GateResult(warnings=(warning,))))
        assert isinstance(decision, Started)  # noqa: S101 - narrows the type for the checker
        self.assertEqual(decision.selected.warnings, (warning,), msg="warning kept")

    def test_gate_problems_refuse_the_link_and_the_chain_moves_on(self) -> None:
        """The refusal keeps the evidence that explains it."""
        problem = Problem(ProblemCode.QUOTA_RESERVE, "89% used")
        gate = FakeGate(GateResult(problems=(problem,), evidence={"quota": {"result": "REFUSED"}}))
        decision = self.route(gate)
        assert isinstance(decision, Started)  # noqa: S101 - narrows the type for the checker
        self.assertEqual(decision.provider, "anthropic", msg="next link")
        refused = decision.evaluations[0]
        self.assertEqual(refused.problems, (problem,), msg="reason kept")
        self.assertEqual(refused.evidence, {"quota": {"result": "REFUSED"}}, msg="evidence kept")

    def test_the_gate_is_not_asked_about_a_link_that_is_already_refused(self) -> None:
        """No observation is spent on a provider that cannot run anyway."""
        for label, gate, kwargs in (
            ("disabled", FakeGate(self.OK), {"enabled": False}),
            ("full", FakeGate(self.OK), {"occupancy": FakeOccupancy(openai=5)}),
        ):
            with self.subTest(label):
                self.route(gate, **kwargs)
                self.assertEqual(gate.asked, [], msg="not consulted")

    def test_an_implementation_without_a_gate_is_judged_by_its_plugin_alone(self) -> None:
        """Gates are optional, like plugins are open."""
        gate = FakeGate(GateResult(problems=(Problem(ProblemCode.QUOTA_RESERVE, "x"),)))
        decision = self.route(gate)
        assert isinstance(decision, Started)  # noqa: S101 - narrows the type for the checker
        self.assertEqual(decision.evaluations[-1].evidence, {}, msg="claude has no gate")
        self.assertEqual(gate.asked, ["openai"], msg="asked once, only for its own link")


class OpenClosedTests(unittest.TestCase):
    """A provider is added by registering a plugin; the router and the domain do not change."""

    def test_a_third_provider_routes_without_touching_the_router(self) -> None:
        """The test's composition root registers the fake and declares its settings."""
        providers = {**default_providers(), "third": provider(FAKE, "third-model", effort="low")}
        fake = SpyPlugin()
        task = default_task(
            ProviderLink("openai"), ProviderLink("third"), ProviderLink("anthropic")
        )
        decision = make_router({CLAUDE: SpyPlugin(), FAKE: fake}, providers).route(task)
        assert isinstance(decision, Started)  # noqa: S101 - narrows the type for the checker
        self.assertEqual(
            (decision.provider, decision.model, decision.effort),
            ("third", "third-model", "low"),
            msg="routed to the fake third provider",
        )
        self.assertEqual(len(fake.asked), 1, msg="the fake plugin was asked")

    def test_the_router_and_the_domain_name_no_provider(self) -> None:
        """Provider ids live in settings and composition roots, never in the routing code."""
        root = Path(agents_dispatcher.__file__).parent
        for relative in ("application/routing.py", "domain/routing.py"):
            text = (root / relative).read_text(encoding="utf-8").lower()
            for vendor in ("openai", "anthropic", "claude", "codex"):
                self.assertNotIn(vendor, text, msg=f"{relative} names {vendor}")


if __name__ == "__main__":
    unittest.main()
