"""Tests of the tool endpoint: arguments in, public payloads out, the same as the command line."""

import json
import unittest

from agents_dispatcher.application.errors import (
    NoProviderEligibleError,
    ReportUnavailableError,
    UnknownRunError,
    WriteTaskNotAllowedError,
)
from agents_dispatcher.application.public_views import rejection_payload
from agents_dispatcher.application.run_queries import ReportResult
from agents_dispatcher.application.schema_check import check
from agents_dispatcher.application.tool_endpoint import ServicesToolEndpoint
from agents_dispatcher.application.tool_outcome import ToolOutcome
from agents_dispatcher.domain.routing import Recommendation, Rejected, TaskMode
from agents_dispatcher.domain.run import RunState, TerminalReason
from tests.support.cli_book import (
    PENDING,
    Book,
    ended,
    invoke,
    make_report,
    make_status,
    report_text,
)
from tests.support.routing import REFUSED
from tests.support.run_spies import RUN_ID

WAIT_MAX_S = 20.0
RECOMMENDATION = Recommendation(
    (REFUSED,), model="gpt-6.1-sol", effort="medium", mode=TaskMode.READ, task_type="audit"
)
REJECTED = Rejected((REFUSED,), task_type="audit")
RUN = RUN_ID.value


def never() -> bool:
    """Report that the caller never cancels."""
    return False


def make_endpoint(book: Book) -> ServicesToolEndpoint:
    """Build an endpoint over the book's services."""
    return ServicesToolEndpoint(
        services=book.services(), wait_max_s=WAIT_MAX_S, parse_json=json.loads
    )


def output_schema(endpoint: ServicesToolEndpoint, name: str) -> dict[str, object]:
    """Return the output schema a tool publishes."""
    for tool in endpoint.tools():
        if tool["name"] == name:
            schema = tool["outputSchema"]
            assert isinstance(schema, dict)  # noqa: S101 - narrows the type
            return schema
    message = f"no tool {name}"
    raise AssertionError(message)


class Rig:
    """An endpoint over fakes with helpers that also check the published output schema."""

    def __init__(self) -> None:
        """Build the book and the endpoint."""
        self.book = Book()
        self.endpoint = make_endpoint(self.book)

    def call(self, name: str, arguments: object, cancelled: object = never) -> ToolOutcome:
        """Call a tool and check that its payload conforms to its output schema."""
        outcome = self.endpoint.call(name, arguments, cancelled)  # type: ignore[arg-type]
        violation = check(output_schema(self.endpoint, name), outcome.payload)
        assert violation is None, f"{name} output breaks its schema: {violation}"  # noqa: S101
        return outcome


def error_code(outcome: ToolOutcome) -> object:
    """Return the code of a failure payload."""
    error = outcome.payload["error"]
    assert isinstance(error, dict)  # noqa: S101 - narrows the type
    return error["code"]


class ListingTests(unittest.TestCase):
    """What `tools/list` shows."""

    def test_the_six_tools_are_listed_with_object_schemas(self) -> None:
        """Names, order and the top-level shape clients require."""
        tools = Rig().endpoint.tools()
        self.assertEqual(
            [tool["name"] for tool in tools],
            ["route", "start", "status", "wait", "stop", "report"],
            msg="tools",
        )
        for tool in tools:
            for key in ("inputSchema", "outputSchema"):
                schema = tool[key]
                assert isinstance(schema, dict)  # noqa: S101 - narrows the type
                self.assertEqual(schema["type"], "object", msg=f"{tool['name']} {key}")
            self.assertTrue(tool["description"], msg=f"{tool['name']} is described")

    def test_start_requires_a_provider_and_route_has_none(self) -> None:
        """The difference between the two submissions."""
        by_name = {tool["name"]: tool for tool in Rig().endpoint.tools()}
        start = by_name["start"]["inputSchema"]
        route = by_name["route"]["inputSchema"]
        assert isinstance(start, dict) and isinstance(route, dict)  # noqa: S101
        self.assertIn("provider", start["required"], msg="start needs a provider")
        self.assertNotIn("provider", route["properties"], msg="route offers no provider")

    def test_the_wait_bound_is_published(self) -> None:
        """The schema carries the actual maximum."""
        by_name = {tool["name"]: tool for tool in Rig().endpoint.tools()}
        schema = by_name["wait"]["inputSchema"]
        assert isinstance(schema, dict)  # noqa: S101 - narrows the type
        self.assertEqual(schema["properties"]["timeout_s"]["maximum"], WAIT_MAX_S, msg="maximum")


class SubmitTests(unittest.TestCase):
    """route and start."""

    def test_start_hands_the_specification_over_and_returns_the_started_payload(self) -> None:
        """Same payload as the command line."""
        rig = Rig()
        outcome = rig.call(
            "start",
            {
                "repo": "/r",
                "task": "t",
                "provider": "anthropic",
                "task_type": "audit",
                "hard_s": 5,
                "memory_bytes": 1000,
            },
        )
        self.assertFalse(outcome.is_error, msg="not an error")
        self.assertEqual(outcome.payload, {"run_id": RUN, "state": "STARTING"}, msg="payload")
        spec = rig.book.start.specs[0]
        self.assertEqual(
            (spec.repo, spec.task, spec.provider, spec.task_type, spec.hard_s, spec.memory_bytes),
            ("/r", "t", "anthropic", "audit", 5, 1000),
            msg="specification",
        )

    def test_route_leaves_the_provider_to_the_chain(self) -> None:
        """No provider argument, none in the specification."""
        rig = Rig()
        rig.call("route", {"repo": "/r", "task": "t"})
        self.assertIsNone(rig.book.start.specs[0].provider, msg="the chain decides")

    def test_a_recommendation_is_data_not_an_error(self) -> None:
        """Exit code 0 on the command line."""
        rig = Rig()
        rig.book.start.result = RECOMMENDATION
        outcome = rig.call("route", {"repo": "/r", "task": "t"})
        self.assertFalse(outcome.is_error, msg="data")
        self.assertEqual(outcome.payload["decision"], "RECOMMENDATION", msg="decision")

    def test_a_rejection_is_an_error_that_carries_the_evaluated_links(self) -> None:
        """Exit code 20 on the command line."""
        rig = Rig()
        rig.book.start.error = NoProviderEligibleError(REJECTED)
        outcome = rig.call("route", {"repo": "/r", "task": "t"})
        self.assertTrue(outcome.is_error, msg="error")
        self.assertEqual(error_code(outcome), "NO_PROVIDER_ELIGIBLE", msg="code")
        self.assertEqual(outcome.payload["rejection"], rejection_payload(REJECTED), msg="links")

    def test_a_forbidden_write_task_is_an_authority_error(self) -> None:
        """Exit code 24 on the command line."""
        rig = Rig()
        rig.book.start.error = WriteTaskNotAllowedError("task type x is not allowed")
        self.assertEqual(
            error_code(rig.call("route", {"repo": "/r", "task": "t"})),
            "AUTHORITY_VIOLATION",
            msg="authority",
        )

    def test_a_blank_task_is_an_invalid_argument_and_nothing_starts(self) -> None:
        """TaskSpec refuses it before any use case runs."""
        rig = Rig()
        outcome = rig.call("route", {"repo": "/r", "task": "   "})
        self.assertEqual(error_code(outcome), "INVALID_ARGUMENT", msg="invalid")
        self.assertEqual(rig.book.start.specs, [], msg="the use case was not called")


class ArgumentTests(unittest.TestCase):
    """Arguments are judged against the published schema."""

    def test_arguments_that_are_not_an_object_are_invalid(self) -> None:
        """A list, a string, null."""
        for arguments in ([1], "x", None):
            outcome = Rig().call("status", arguments)
            self.assertEqual(error_code(outcome), "INVALID_ARGUMENT", msg=f"{arguments!r}")

    def test_missing_unknown_and_mistyped_arguments_are_named(self) -> None:
        """The message names the member."""
        rig = Rig()
        cases: tuple[tuple[str, dict[str, object], str], ...] = (
            ("status", {}, "run_id"),
            ("status", {"run_id": RUN, "extra": 1}, "extra"),
            ("status", {"run_id": 5}, "run_id"),
            ("route", {"repo": "/r", "task": "t", "hard_s": 0}, "hard_s"),
            ("start", {"repo": "/r", "task": "t"}, "provider"),
        )
        for name, arguments, member in cases:
            outcome = rig.call(name, arguments)
            self.assertEqual(error_code(outcome), "INVALID_ARGUMENT", msg=f"{name} {arguments}")
            error = outcome.payload["error"]
            assert isinstance(error, dict)  # noqa: S101 - narrows the type
            self.assertIn(member, str(error["message"]), msg=f"{name} names {member}")
        self.assertEqual(rig.book.status.asked, [], msg="no use case ran")
        self.assertEqual(rig.book.start.specs, [], msg="no submission happened")

    def test_a_run_id_that_is_not_a_run_id_is_invalid(self) -> None:
        """Only canonical ids."""
        outcome = Rig().call("status", {"run_id": "not-a-run"})
        self.assertEqual(error_code(outcome), "INVALID_ARGUMENT", msg="invalid")


class QueryTests(unittest.TestCase):
    """status, wait, stop, report."""

    def test_status_is_the_command_line_payload(self) -> None:
        """Same dict."""
        rig = Rig()
        outcome = rig.call("status", {"run_id": RUN})
        self.assertFalse(outcome.is_error, msg="data")
        self.assertEqual(outcome.payload, make_status().as_dict(), msg="payload")
        self.assertEqual(rig.book.status.asked, [RUN_ID], msg="asked for the run")

    def test_an_unknown_run_is_an_error(self) -> None:
        """Exit code 21."""
        rig = Rig()
        rig.book.status.error = UnknownRunError(RUN)
        self.assertEqual(error_code(rig.call("status", {"run_id": RUN})), "UNKNOWN_RUN", msg="code")

    def test_wait_defaults_to_the_maximum_and_passes_the_cancel_probe(self) -> None:
        """The probe reaches the use case unchanged."""
        rig = Rig()
        flag = {"cancelled": False}
        rig.call("wait", {"run_id": RUN}, lambda: flag["cancelled"])
        self.assertEqual(rig.book.wait.calls, [(RUN_ID, WAIT_MAX_S)], msg="default timeout")
        probe = rig.book.wait.stop_probes[0]
        assert probe is not None  # noqa: S101 - narrows the type
        self.assertFalse(probe(), msg="not cancelled")
        flag["cancelled"] = True
        self.assertTrue(probe(), msg="the probe follows the caller")

    def test_wait_takes_an_explicit_timeout_up_to_the_maximum(self) -> None:
        """The bound is inclusive."""
        rig = Rig()
        rig.call("wait", {"run_id": RUN, "timeout_s": WAIT_MAX_S})
        self.assertEqual(rig.book.wait.calls[0][1], WAIT_MAX_S, msg="maximum accepted")

    def test_a_timeout_above_the_maximum_names_the_setting(self) -> None:
        """Never clamped silently."""
        rig = Rig()
        outcome = rig.call("wait", {"run_id": RUN, "timeout_s": WAIT_MAX_S + 1})
        self.assertEqual(error_code(outcome), "INVALID_ARGUMENT", msg="invalid")
        error = outcome.payload["error"]
        assert isinstance(error, dict)  # noqa: S101 - narrows the type
        self.assertIn("mcp.wait_max_s", str(error["message"]), msg="names the key")
        self.assertEqual(rig.book.wait.calls, [], msg="nothing waited")

    def test_pending_and_every_run_outcome_are_data(self) -> None:
        """Exit codes 3 and 10-17 on the command line are data here."""
        for result in (PENDING, ended(TerminalReason.COMPLETED), ended(TerminalReason.FAILED)):
            rig = Rig()
            rig.book.wait.steps = [result]
            outcome = rig.call("wait", {"run_id": RUN})
            self.assertFalse(outcome.is_error, msg=f"{result.ready} is data")
            self.assertEqual(
                outcome.payload["result"], "READY" if result.ready else "PENDING", msg="result"
            )

    def test_stop_passes_the_reason_and_refuses_an_empty_one(self) -> None:
        """The reason is required."""
        rig = Rig()
        outcome = rig.call("stop", {"run_id": RUN, "reason": "because"})
        self.assertEqual(outcome.payload["disposition"], "EFFECTIVE", msg="receipt")
        self.assertEqual(rig.book.stop.calls, [(RUN_ID, "because")], msg="reason passed")
        self.assertEqual(
            error_code(rig.call("stop", {"run_id": RUN, "reason": ""})),
            "INVALID_ARGUMENT",
            msg="empty reason",
        )

    def test_a_reason_the_use_case_refuses_is_an_invalid_argument(self) -> None:
        """A ValueError from the use case is the caller's mistake."""
        rig = Rig()
        rig.book.stop.error = ValueError("reason is too long")
        self.assertEqual(
            error_code(rig.call("stop", {"run_id": RUN, "reason": "x"})),
            "INVALID_ARGUMENT",
            msg="invalid",
        )

    def test_a_finished_report_is_the_stored_object(self) -> None:
        """What the command line prints, parsed."""
        rig = Rig()
        stored = report_text(make_report(TerminalReason.COMPLETED))
        rig.book.report.result = ReportResult(report_json=stored, pending=None)
        self.assertEqual(
            rig.call("report", {"run_id": RUN}).payload, json.loads(stored), msg="same"
        )

    def test_an_unfinished_report_is_pending_data(self) -> None:
        """Exit code 3 on the command line."""
        rig = Rig()
        rig.book.report.result = ReportResult(
            report_json=None, pending=make_status(RunState.RUNNING)
        )
        outcome = rig.call("report", {"run_id": RUN})
        self.assertFalse(outcome.is_error, msg="data")
        self.assertEqual(outcome.payload["result"], "PENDING", msg="pending")

    def test_a_missing_report_is_an_error(self) -> None:
        """Exit code 25."""
        rig = Rig()
        rig.book.report.error = ReportUnavailableError("gone")
        self.assertEqual(
            error_code(rig.call("report", {"run_id": RUN})), "REPORT_UNAVAILABLE", msg="code"
        )

    def test_an_unexpected_failure_is_an_error_named_failure(self) -> None:
        """Nothing escapes the endpoint."""
        rig = Rig()
        rig.book.status.error = RuntimeError("boom")
        self.assertEqual(error_code(rig.call("status", {"run_id": RUN})), "FAILURE", msg="code")


class SameAsTheCommandLineTests(unittest.TestCase):
    """One source: the command line and the tools print the same objects."""

    def cli_json(self, argv: list[str], book: Book) -> object:
        """Run the command line against the book and parse its standard output."""
        return json.loads(invoke(argv, book).stdout)

    def test_each_operation_prints_the_same_object_on_both_front_ends(self) -> None:
        """status, start, wait (both ends), stop, report, recommendation."""
        stored = report_text(make_report(TerminalReason.COMPLETED))
        scenarios = (
            ("status", ["status", RUN], {"run_id": RUN}, None),
            (
                "start",
                ["start", "--repo", "/r", "--task", "t", "--provider", "anthropic"],
                {"repo": "/r", "task": "t", "provider": "anthropic"},
                None,
            ),
            ("wait", ["wait", RUN], {"run_id": RUN}, None),
            ("wait", ["wait", RUN, "--timeout-s", "1"], {"run_id": RUN, "timeout_s": 1}, PENDING),
            ("stop", ["stop", RUN, "--reason", "r"], {"run_id": RUN, "reason": "r"}, None),
            ("report", ["report", RUN], {"run_id": RUN}, None),
        )
        for name, argv, arguments, wait_result in scenarios:
            cli_book, mcp_book = Book(), Book()
            for book in (cli_book, mcp_book):
                book.report.result = ReportResult(report_json=stored, pending=None)
                if wait_result is not None:
                    book.wait.steps = [wait_result]
            outcome = make_endpoint(mcp_book).call(name, arguments, never)
            self.assertEqual(
                outcome.payload, self.cli_json(argv, cli_book), msg=f"{name} {argv[0]} same object"
            )

    def test_a_recommendation_and_a_rejection_match_the_command_line(self) -> None:
        """Both non-start answers of a submission."""
        book = Book()
        book.start.result = RECOMMENDATION
        self.assertEqual(
            make_endpoint(book).call("route", {"repo": "/r", "task": "t"}, never).payload,
            self.cli_json(["start", "--repo", "/r", "--task", "t"], book),
            msg="recommendation",
        )
        book = Book()
        book.start.error = NoProviderEligibleError(REJECTED)
        outcome = make_endpoint(book).call("route", {"repo": "/r", "task": "t"}, never)
        self.assertEqual(
            outcome.payload["rejection"],
            self.cli_json(["start", "--repo", "/r", "--task", "t"], book),
            msg="rejection",
        )


if __name__ == "__main__":
    unittest.main()
