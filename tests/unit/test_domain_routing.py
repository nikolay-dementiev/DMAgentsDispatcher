"""Tests of the routing domain: names, task modes, provider links and the validated chain."""

import unittest

from agents_dispatcher.domain.routing import (
    Chain,
    Disposition,
    Evaluation,
    HandoffLink,
    Problem,
    ProblemCode,
    ProviderLink,
    Recommendation,
    Rejected,
    ResolvedTask,
    RunRouting,
    Started,
    TaskMode,
    validate_name,
)


class NameTests(unittest.TestCase):
    """Task type names and provider ids share one grammar."""

    def test_lowercase_digits_and_hyphens_are_accepted(self) -> None:
        """A letter first, then letters, digits and hyphens."""
        for name in ("audit", "a", "security-review-2", "a" * 32):
            with self.subTest(name=name):
                self.assertEqual(validate_name(name, "task type"), name, msg="accepted as is")

    def test_everything_else_is_refused(self) -> None:
        """Uppercase, underscore, leading digit or hyphen, empty, too long, spaces, newline."""
        for name in ("Audit", "_template", "1a", "-a", "", "a" * 33, "a b", "a\n", "a_b", "é"):
            with self.subTest(name=name), self.assertRaises(ValueError, msg=repr(name)):
                validate_name(name, "task type")

    def test_the_message_names_what_was_checked(self) -> None:
        """The caller learns which name was refused."""
        with self.assertRaises(ValueError, msg="refused") as caught:
            validate_name("Bad", "task type")
        self.assertIn("task type", str(caught.exception), msg="names the kind of name")


class TaskModeTests(unittest.TestCase):
    """The two modes carry the words the settings use."""

    def test_values_are_the_settings_words(self) -> None:
        """`read` and `write`, nothing else."""
        self.assertEqual([mode.value for mode in TaskMode], ["read", "write"], msg="two modes")


class ChainTests(unittest.TestCase):
    """A chain is nonempty, has unique providers and a handoff only at its end."""

    def test_providers_then_handoff_is_valid(self) -> None:
        """The architecture default shape."""
        links = (ProviderLink("openai"), ProviderLink("anthropic"), HandoffLink())
        self.assertEqual(Chain(links).links, links, msg="kept in order")

    def test_a_chain_without_a_terminal_link_is_valid(self) -> None:
        """A terminal link is allowed, not required."""
        self.assertEqual(len(Chain((ProviderLink("anthropic"),)).links), 1, msg="one link")

    def test_link_attributes_are_kept(self) -> None:
        """Model and effort are link attributes."""
        link = ProviderLink("anthropic", model="m-1", effort="high")
        self.assertEqual((link.model, link.effort), ("m-1", "high"), msg="attributes kept")

    def test_an_empty_chain_is_refused(self) -> None:
        """Nonempty."""
        with self.assertRaises(ValueError, msg="empty") as caught:
            Chain(())
        self.assertIn("empty", str(caught.exception), msg="says why")

    def test_a_duplicate_provider_is_refused(self) -> None:
        """One link per provider."""
        with self.assertRaises(ValueError, msg="duplicate") as caught:
            Chain((ProviderLink("anthropic"), ProviderLink("anthropic", model="m")))
        self.assertIn("anthropic", str(caught.exception), msg="names the provider")

    def test_a_handoff_that_is_not_last_is_refused(self) -> None:
        """Handoff only last."""
        with self.assertRaises(ValueError, msg="handoff first") as caught:
            Chain((HandoffLink(), ProviderLink("anthropic")))
        self.assertIn("handoff", str(caught.exception), msg="names the handoff")

    def test_two_handoffs_are_refused(self) -> None:
        """The first of two is not last."""
        with self.assertRaises(ValueError, msg="two handoffs"):
            Chain((ProviderLink("anthropic"), HandoffLink(), HandoffLink()))

    def test_a_provider_link_validates_its_id(self) -> None:
        """The id obeys the name grammar."""
        with self.assertRaises(ValueError, msg="bad id"):
            ProviderLink("Anthropic")


class EvaluationTests(unittest.TestCase):
    """One evaluated link is selected, or refused with at least one problem."""

    def test_a_refused_link_lists_its_problems(self) -> None:
        """Code and message, in order."""
        problems = (
            Problem(ProblemCode.PROVIDER_NOT_LOADED, "no implementation"),
            Problem(ProblemCode.PROVIDER_DISABLED, "enabled = false"),
        )
        evaluation = Evaluation("openai", Disposition.REFUSED, problems)
        self.assertEqual(
            evaluation.as_dict(),
            {
                "provider": "openai",
                "disposition": "REFUSED",
                "problems": [
                    {"code": "PROVIDER_NOT_LOADED", "message": "no implementation"},
                    {"code": "PROVIDER_DISABLED", "message": "enabled = false"},
                ],
            },
            msg="refusal as plain values",
        )

    def test_a_selected_link_carries_its_invocation_facts(self) -> None:
        """Model and effort after the link overrides."""
        evaluation = Evaluation("anthropic", Disposition.SELECTED, model="m-1", effort="low")
        self.assertEqual(
            evaluation.as_dict(),
            {
                "provider": "anthropic",
                "disposition": "SELECTED",
                "problems": [],
                "model": "m-1",
                "effort": "low",
            },
            msg="selection as plain values",
        )

    def test_a_refusal_without_a_problem_is_refused(self) -> None:
        """A refusal always says why."""
        with self.assertRaises(ValueError, msg="no problem"):
            Evaluation("openai", Disposition.REFUSED)

    def test_a_selection_with_a_problem_is_refused(self) -> None:
        """A selected link has nothing to complain about."""
        problem = Problem(ProblemCode.PROVIDER_DISABLED, "x")
        with self.assertRaises(ValueError, msg="selected with a problem"):
            Evaluation("openai", Disposition.SELECTED, (problem,), model="m", effort="e")

    def test_a_selection_needs_model_and_effort(self) -> None:
        """The run needs both."""
        with self.assertRaises(ValueError, msg="no model"):
            Evaluation("anthropic", Disposition.SELECTED)

    def test_a_refusal_carries_no_invocation_facts(self) -> None:
        """Nothing will be invoked."""
        problem = Problem(ProblemCode.PROVIDER_DISABLED, "x")
        with self.assertRaises(ValueError, msg="refused with a model"):
            Evaluation("openai", Disposition.REFUSED, (problem,), model="m")


class EvidenceAndWarningTests(unittest.TestCase):
    """A link's account evidence and warnings travel with its evaluation and survive the store."""

    def test_a_selection_carries_warnings_and_evidence_in_its_plain_values(self) -> None:
        """Warnings are problems that did not stop the run; evidence is what was read."""
        warning = Problem(ProblemCode.QUOTA_UNSUPPORTED_ALLOWED, "no quota interface")
        evaluation = Evaluation(
            "anthropic",
            Disposition.SELECTED,
            model="m",
            effort="e",
            warnings=(warning,),
            evidence={"login": {"state": "LOGGED_IN"}},
        )
        data = evaluation.as_dict()
        self.assertEqual(
            data["warnings"],
            [{"code": "QUOTA_UNSUPPORTED_ALLOWED", "message": "no quota interface"}],
            msg="warnings listed",
        )
        self.assertEqual(data["evidence"], {"login": {"state": "LOGGED_IN"}}, msg="evidence")
        self.assertEqual(Evaluation.from_dict(data), evaluation, msg="round trip")

    def test_a_refusal_may_carry_evidence_but_not_warnings(self) -> None:
        """The login read that explains the refusal is shown; warnings belong to selections."""
        problem = Problem(ProblemCode.QUOTA_RESERVE, "89% used")
        refused = Evaluation(
            "openai", Disposition.REFUSED, (problem,), evidence={"quota": {"result": "REFUSED"}}
        )
        self.assertEqual(
            refused.as_dict()["evidence"], {"quota": {"result": "REFUSED"}}, msg="shown"
        )
        with self.assertRaises(ValueError, msg="warning on a refusal"):
            Evaluation("openai", Disposition.REFUSED, (problem,), warnings=(problem,))

    def test_an_evaluation_without_extras_keeps_its_old_plain_values(self) -> None:
        """No empty keys: stored routes from before still read back and print the same."""
        data = Evaluation("anthropic", Disposition.SELECTED, model="m", effort="e").as_dict()
        self.assertNotIn("warnings", data, msg="no empty warnings key")
        self.assertNotIn("evidence", data, msg="no empty evidence key")

    def test_a_damaged_warnings_or_evidence_value_is_refused(self) -> None:
        """A damaged store row is an error, not a guess."""
        base = {"provider": "x", "disposition": "SELECTED", "model": "m", "effort": "e"}
        for extra in ({"warnings": "x"}, {"evidence": [1]}, {"warnings": [{"code": "NOPE"}]}):
            with self.subTest(extra=extra), self.assertRaises(ValueError, msg="damaged"):
                Evaluation.from_dict({**base, **extra})


class ResolvedTaskTests(unittest.TestCase):
    """A resolved task is a task type (or none), a mode and a chain."""

    def test_the_task_type_is_a_valid_name_or_none(self) -> None:
        """No type means the defaults resolved it."""
        chain = Chain((ProviderLink("anthropic"),))
        self.assertIsNone(ResolvedTask(None, TaskMode.READ, chain).task_type, msg="no type")
        with self.assertRaises(ValueError, msg="bad type"):
            ResolvedTask("Audit", TaskMode.READ, chain)


class DecisionTests(unittest.TestCase):
    """The three decisions and the plain values the CLI prints."""

    def setUp(self) -> None:
        """One refusal and one selection to build routes from."""
        problem = Problem(ProblemCode.PROVIDER_NOT_LOADED, "no implementation")
        self.refused = Evaluation("openai", Disposition.REFUSED, (problem,))
        self.selected = Evaluation("anthropic", Disposition.SELECTED, model="m-1", effort="low")

    def test_started_names_the_provider_model_and_effort(self) -> None:
        """What the run will use."""
        decision = Started(self.selected, (self.refused, self.selected))
        self.assertEqual(
            (decision.provider, decision.model, decision.effort),
            ("anthropic", "m-1", "low"),
            msg="invocation facts of the selected link",
        )

    def test_a_recommendation_is_labelled_and_lists_the_refusals(self) -> None:
        """Field names follow DecisionV1: kind, model, effort, requirements, refusals, boundary."""
        decision = Recommendation(
            (self.refused,), model="gpt-x", effort="medium", mode=TaskMode.READ, task_type="audit"
        )
        self.assertEqual(
            decision.as_dict(),
            {
                "decision": "RECOMMENDATION",
                "task_type": "audit",
                "model": "gpt-x",
                "effort": "medium",
                "requirements": {"mode": "read"},
                "refusals": [self.refused.as_dict()],
                "boundary": "OUTSIDE_SDK_GUARANTEES",
            },
            msg="recommendation as plain values",
        )

    def test_a_rejection_carries_the_problem_and_the_context(self) -> None:
        """Field names follow DecisionV1: kind, problems, context."""
        decision = Rejected((self.refused,), task_type=None)
        self.assertEqual(
            decision.as_dict(),
            {
                "decision": "REJECTED",
                "task_type": None,
                "problems": [
                    {
                        "code": "NO_PROVIDER_ELIGIBLE",
                        "message": "every execution link in the chain refused the task",
                    }
                ],
                "context": [self.refused.as_dict()],
            },
            msg="rejection as plain values",
        )


class RunRoutingTests(unittest.TestCase):
    """What a run keeps of its decision, and how a stored route is read back."""

    def test_a_started_decision_becomes_the_routing_of_the_run(self) -> None:
        """Task type, mode, the selected provider with model and effort, and the route."""
        refused = Evaluation(
            "openai", Disposition.REFUSED, (Problem(ProblemCode.PROVIDER_NOT_LOADED, "x"),)
        )
        selected = Evaluation("anthropic", Disposition.SELECTED, model="m-1", effort="low")
        task = ResolvedTask("audit", TaskMode.READ, Chain((ProviderLink("anthropic"),)))
        routing = RunRouting.from_started(task, Started(selected, (refused, selected)))
        self.assertEqual(
            (routing.task_type, routing.mode, routing.provider, routing.model, routing.effort),
            ("audit", TaskMode.READ, "anthropic", "m-1", "low"),
            msg="the facts of the run",
        )
        self.assertEqual(routing.route, (refused, selected), msg="the route, in chain order")

    def test_an_evaluation_is_read_back_from_its_plain_values(self) -> None:
        """The store keeps the route as plain values."""
        problem = Problem(ProblemCode.EXECUTABLE_UNAVAILABLE, "no tool")
        for evaluation in (
            Evaluation("openai", Disposition.REFUSED, (problem,)),
            Evaluation("anthropic", Disposition.SELECTED, model="m", effort="e"),
        ):
            with self.subTest(provider=evaluation.provider):
                self.assertEqual(
                    Evaluation.from_dict(evaluation.as_dict()), evaluation, msg="round trip"
                )

    def test_plain_values_that_are_not_an_evaluation_are_refused(self) -> None:
        """A damaged store row is an error, not a guess."""
        for damaged in ({}, {"provider": "x"}, {"provider": "x", "disposition": "MAYBE"}):
            with self.subTest(damaged=damaged), self.assertRaises(ValueError, msg="damaged"):
                Evaluation.from_dict(damaged)
