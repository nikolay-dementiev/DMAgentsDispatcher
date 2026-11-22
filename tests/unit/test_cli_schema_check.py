"""What a command prints conforms to the schema of the MCP tool that answers the same question.

Owner decision 2026-11-22 (KICKOFF-13): the quarantine answer has a schema constant beside the
tool schemas, and every command-line unit scenario is checked against the schema of its command.
An output that no schema names fails the check; nothing is skipped without a stated reason.
"""

import json
import unittest

from agents_dispatcher.application.public_views import quarantine_payload
from agents_dispatcher.application.quarantine_resolution import Resolution, ResolutionOutcome
from agents_dispatcher.application.schema_check import check
from agents_dispatcher.application.tool_schemas import FAILURE_SCHEMA, QUARANTINE_RESOLVE_SCHEMA
from tests.support.cli_schema_check import assert_conforms
from tests.support.run_spies import RUN_ID

ID = RUN_ID.value
STARTED = json.dumps({"run_id": ID, "state": "STARTING"})
FAILURE = json.dumps({"error": {"code": "FAILURE", "message": "x"}})


class QuarantineSchemaTests(unittest.TestCase):
    """The constant says what `quarantine resolve` prints."""

    def test_the_payload_of_every_outcome_conforms(self) -> None:
        """A settled run and a held one."""
        for outcome in ResolutionOutcome:
            with self.subTest(outcome=outcome.name):
                payload = quarantine_payload(Resolution(RUN_ID, outcome))
                self.assertIsNone(check(QUARANTINE_RESOLVE_SCHEMA, payload), msg="conforms")

    def test_a_missing_member_and_an_extra_member_are_refused(self) -> None:
        """The schema is closed and complete."""
        payload = quarantine_payload(Resolution(RUN_ID, ResolutionOutcome.SETTLED))
        without = {key: value for key, value in payload.items() if key != "outcome"}
        self.assertIsNotNone(check(QUARANTINE_RESOLVE_SCHEMA, without), msg="outcome is required")
        self.assertIsNotNone(
            check(QUARANTINE_RESOLVE_SCHEMA, {**payload, "extra": 1}), msg="no extra member"
        )

    def test_the_failure_schema_wants_a_code(self) -> None:
        """The failure shape every tool shares."""
        good = {"error": {"code": "FAILURE", "message": "x"}}
        self.assertIsNone(check(FAILURE_SCHEMA, good), msg="a failure object conforms")
        self.assertIsNotNone(check(FAILURE_SCHEMA, {"error": {"message": "x"}}), msg="code needed")


class HookTests(unittest.TestCase):
    """The check of a command's stdout, and what it refuses to skip."""

    def test_each_command_accepts_its_own_object_and_the_failure_object(self) -> None:
        """A started run for `start`; a failure object for any command."""
        assert_conforms(["start", "--repo", "r", "--task", "t"], STARTED)
        assert_conforms(["status", ID], FAILURE)
        assert_conforms(["quarantine", "resolve", ID], FAILURE)

    def test_an_object_of_the_wrong_command_is_refused(self) -> None:
        """A started run is not a status."""
        with self.assertRaises(AssertionError, msg="a started run is no status"):
            assert_conforms(["status", ID], STARTED)

    def test_output_that_is_not_json_is_refused_unless_the_format_says_text(self) -> None:
        """Text on stdout is explained by `--format text`, never by itself."""
        with self.assertRaises(AssertionError, msg="stdout is not JSON"):
            assert_conforms(["status", ID], "run is RUNNING\n")
        assert_conforms(["status", "--format", "text", ID], "run is RUNNING\n")

    def test_empty_output_is_refused_for_a_command_that_prints_an_object(self) -> None:
        """A silent command is a defect."""
        with self.assertRaises(AssertionError, msg="nothing printed"):
            assert_conforms(["status", ID], "")

    def test_help_and_version_are_explained_skips(self) -> None:
        """They print text by design."""
        assert_conforms(["status", "--help"], "usage: ...\n")
        assert_conforms(["help", "status"], "usage: ...\n")
        assert_conforms(["--version"], "agentsd 1.0\n")


if __name__ == "__main__":
    unittest.main()
