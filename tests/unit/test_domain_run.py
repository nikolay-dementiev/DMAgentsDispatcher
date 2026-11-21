"""Tests of the run domain: identifiers, states, terminal reasons, task spec and report."""

import unittest

from agents_dispatcher.domain.run import (
    RESULT_TEXT_LIMIT,
    LimitKind,
    Limits,
    RunId,
    RunReport,
    RunState,
    TaskSpec,
    TerminalReason,
)
from tests.support.routing import ROUTING

VALID_ID = "3f2b8c1e-5d4a-4b7e-9c3a-1f2e3d4c5b6a"


def make_report(**changes: object) -> RunReport:
    """Return a completed report; keyword arguments override single fields."""
    fields: dict[str, object] = {
        "run_id": RunId(VALID_ID),
        "state": RunState.TERMINAL,
        "terminal_reason": TerminalReason.COMPLETED,
        "repo": "/work/example",
        "base_commit": "a" * 40,
        "branch": f"agentsd-run-{VALID_ID}",
        "commits_on_branch": 1,
        "worker_cli_version": "2.1.304",
        "worker_model": "claude-sonnet-5-5",
        "worker_model_source": "reported",
        "worker_exit_status": 0,
        "duration_s": 12.5,
        "limits_configured": Limits(hard_s=60.0, memory_bytes=1024),
        "limit_hit": None,
        "survivors": (),
        "result_text": "done",
        "notes": (),
        "routing": ROUTING,
    }
    fields.update(changes)
    return RunReport(**fields)  # type: ignore[arg-type]


class RunIdTests(unittest.TestCase):
    """A run identifier is a canonical random UUID and names the run's resources."""

    def test_a_canonical_uuid_is_accepted_and_names_the_resources(self) -> None:
        """The sandbox directory and the result branch share one name."""
        run_id = RunId(VALID_ID)
        self.assertEqual(run_id.sandbox_name, f"agentsd-run-{VALID_ID}", msg="sandbox name")
        self.assertEqual(run_id.branch_name, f"agentsd-run-{VALID_ID}", msg="branch name")

    def test_anything_else_is_rejected(self) -> None:
        """Wrong case, wrong version, path tricks and empty values all fail."""
        bad = [
            "",
            "not-a-uuid",
            VALID_ID.upper(),
            VALID_ID.replace("-4b7e-", "-1b7e-"),
            f"../{VALID_ID}",
            f"{VALID_ID}\n",
        ]
        for value in bad:
            with self.subTest(value=value), self.assertRaises(ValueError):
                RunId(value)


class VocabularyTests(unittest.TestCase):
    """States and terminal reasons use the architecture's names."""

    def test_run_states_match_the_architecture(self) -> None:
        """ARCH L746-747 minus the two states M1 does not reach, plus the quarantine."""
        expected = {
            "ACCEPTED",
            "STARTING",
            "RUNNING",
            "STOPPING",
            "FINALIZING",
            "QUARANTINED",
            "TERMINAL",
        }
        self.assertEqual({state.name for state in RunState}, expected, msg="state names")

    def test_terminal_reasons_match_the_architecture(self) -> None:
        """ARCH L808 and L1432 names that M1 can reach."""
        expected = {
            "COMPLETED",
            "FAILED",
            "RESOURCE_LIMIT",
            "STOPPED",
            "ORPHANED",
            "LAUNCH_LOST",
        }
        self.assertEqual({reason.name for reason in TerminalReason}, expected, msg="reason names")

    def test_limit_kinds_are_the_setting_names(self) -> None:
        """A hit limit is reported by the settings key it came from."""
        self.assertEqual(LimitKind.TIME.value, "watchdog.hard_s", msg="time limit key")
        self.assertEqual(
            LimitKind.MEMORY.value, "resources.run.memory_bytes", msg="memory limit key"
        )


class SpecTests(unittest.TestCase):
    """Task specs and limits refuse nonsense before anything starts."""

    def test_a_valid_spec_is_accepted(self) -> None:
        """Repository and task text are enough; overrides are optional."""
        spec = TaskSpec(repo="/work/example", task="fix the bug")
        self.assertIsNone(spec.hard_s, msg="no time override by default")
        self.assertIsNone(spec.memory_bytes, msg="no memory override by default")
        self.assertIsNone(spec.task_type, msg="no task type by default")
        self.assertIsNone(spec.provider, msg="no provider restriction by default")

    def test_a_task_type_and_a_provider_are_kept_when_valid(self) -> None:
        """Both follow the name grammar of the settings."""
        spec = TaskSpec(repo="/r", task="x", task_type="audit", provider="anthropic")
        self.assertEqual((spec.task_type, spec.provider), ("audit", "anthropic"), msg="kept")

    def test_invalid_specs_are_rejected(self) -> None:
        """Blank text and non-positive overrides fail with ValueError."""
        cases = [
            {"repo": " ", "task": "x"},
            {"repo": "/r", "task": "  "},
            {"repo": "/r", "task": "x", "hard_s": 0.0},
            {"repo": "/r", "task": "x", "memory_bytes": -1},
            {"repo": "/r", "task": "bad \udc80 byte"},
            {"repo": "/r", "task": "x", "task_type": "Audit"},
            {"repo": "/r", "task": "x", "provider": "_template"},
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                TaskSpec(**case)  # type: ignore[arg-type]

    def test_limits_must_be_positive(self) -> None:
        """A zero limit would stop every run at once."""
        with self.assertRaises(ValueError):
            Limits(hard_s=0.0, memory_bytes=1)
        with self.assertRaises(ValueError):
            Limits(hard_s=1.0, memory_bytes=0)


class ReportTests(unittest.TestCase):
    """The report is the JSON contract of `agentsd run` (M1-provisional)."""

    def test_the_dictionary_has_exactly_the_contract_fields(self) -> None:
        """Field names and nesting follow KICKOFF-4 section 3."""
        data = make_report().as_dict()
        self.assertEqual(
            list(data),
            [
                "run_id",
                "state",
                "terminal_reason",
                "task_type",
                "mode",
                "provider",
                "repo",
                "base_commit",
                "branch",
                "commits_on_branch",
                "worker",
                "route",
                "duration_s",
                "limits",
                "survivors",
                "result_text",
                "notes",
            ],
            msg="top-level fields in contract order",
        )
        self.assertEqual(
            data["worker"],
            {
                "cli_version": "2.1.304",
                "exit_status": 0,
                "model": "claude-sonnet-5-5",
                "model_source": "reported",
            },
            msg="worker block",
        )
        self.assertEqual(
            (data["task_type"], data["mode"], data["provider"]),
            ("implement", "write", "anthropic"),
            msg="what ran",
        )
        self.assertEqual(
            data["route"], [item.as_dict() for item in ROUTING.route], msg="every evaluated link"
        )
        self.assertEqual(
            data["limits"],
            {"configured": {"hard_s": 60.0, "memory_bytes": 1024}, "hit": None},
            msg="limits block",
        )

    def test_enums_are_reported_by_name(self) -> None:
        """Consumers read names, never numbers."""
        data = make_report(
            terminal_reason=TerminalReason.RESOURCE_LIMIT, limit_hit=LimitKind.MEMORY
        ).as_dict()
        self.assertEqual(data["state"], "TERMINAL", msg="state name")
        self.assertEqual(data["terminal_reason"], "RESOURCE_LIMIT", msg="reason name")
        self.assertEqual(
            data["limits"]["hit"],  # type: ignore[index]
            "resources.run.memory_bytes",
            msg="hit limit key",
        )

    def test_a_run_without_changes_has_no_branch(self) -> None:
        """Q4: branch is null and the commit count is zero."""
        data = make_report(branch=None, commits_on_branch=0).as_dict()
        self.assertIsNone(data["branch"], msg="no branch")
        self.assertEqual(data["commits_on_branch"], 0, msg="no commits")

    def test_result_text_is_bounded(self) -> None:
        """A long worker answer is cut at the limit and says so."""
        data = make_report(result_text="x" * (RESULT_TEXT_LIMIT + 50)).as_dict()
        text = data["result_text"]
        notes = data["notes"]
        assert isinstance(text, str)  # noqa: S101 - narrows the type for the checker
        assert isinstance(notes, list)  # noqa: S101 - narrows the type for the checker
        self.assertLessEqual(len(text), RESULT_TEXT_LIMIT, msg="text is cut")
        self.assertIn("result_text truncated", notes, msg="truncation noted")
