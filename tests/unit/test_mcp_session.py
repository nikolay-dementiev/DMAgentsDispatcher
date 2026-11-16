"""Tests of the MCP lifecycle: what the server answers before and after `initialize`."""

import unittest

from agents_dispatcher.interfaces.mcp.session import (
    CallTool,
    Cancel,
    Log,
    Reply,
    Session,
)

from agents_dispatcher.interfaces.mcp.protocol import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    Notification,
    Request,
    Unsolicited,
)

TOOLS: list[dict[str, object]] = [{"name": "status"}, {"name": "wait"}]


def make_session() -> Session:
    """Build a session over two fake tools."""
    return Session(server_version="1.2.3", tools=TOOLS)


def initialize(
    session: Session, version: object = "2025-06-18", request_id: int = 1
) -> list[object]:
    """Send `initialize` and return the actions."""
    params: dict[str, object] = {
        "protocolVersion": version,
        "capabilities": {},
        "clientInfo": {"name": "client", "version": "9"},
    }
    return list(session.handle(Request(request_id, "initialize", params)))


def result_of(actions: list[object]) -> dict[str, object]:
    """Return the result of the single Reply in `actions`."""
    (reply,) = [item for item in actions if isinstance(item, Reply)]
    result = reply.message["result"]
    assert isinstance(result, dict)  # noqa: S101 - narrows the type
    return result


def error_code(actions: list[object]) -> object:
    """Return the error code of the single Reply in `actions`."""
    (reply,) = [item for item in actions if isinstance(item, Reply)]
    error = reply.message["error"]
    assert isinstance(error, dict)  # noqa: S101 - narrows the type
    return error["code"]


class InitializeTests(unittest.TestCase):
    """Version negotiation and the answer to `initialize`."""

    def test_a_supported_version_is_echoed(self) -> None:
        """Both revisions."""
        for version in ("2025-06-18", "2025-11-25"):
            result = result_of(initialize(make_session(), version))
            self.assertEqual(result["protocolVersion"], version, msg=f"{version} echoed")

    def test_any_other_version_gets_the_latest_supported(self) -> None:
        """The server answers with a version it speaks."""
        for version in ("2024-11-05", "2099-01-01", "nonsense"):
            result = result_of(initialize(make_session(), version))
            self.assertEqual(result["protocolVersion"], "2025-11-25", msg=f"{version} answered")

    def test_the_answer_names_the_server_and_its_one_capability(self) -> None:
        """Tools only, no list-changed notifications."""
        result = result_of(initialize(make_session()))
        self.assertEqual(
            result["serverInfo"],
            {"name": "agents-dispatcher", "version": "1.2.3"},
            msg="server info",
        )
        self.assertEqual(
            result["capabilities"], {"tools": {"listChanged": False}}, msg="capabilities"
        )

    def test_a_missing_or_non_text_version_is_invalid_params(self) -> None:
        """-32602, and the session stays uninitialized."""
        session = make_session()
        self.assertEqual(
            error_code(list(session.handle(Request(1, "initialize", {})))),
            INVALID_PARAMS,
            msg="missing",
        )
        self.assertEqual(error_code(initialize(session, version=5)), INVALID_PARAMS, msg="number")
        self.assertEqual(
            error_code(list(session.handle(Request(3, "tools/list", {})))),
            INVALID_REQUEST,
            msg="still uninitialized after the failures",
        )

    def test_a_second_initialize_is_refused(self) -> None:
        """Once per connection."""
        session = make_session()
        initialize(session)
        self.assertEqual(
            error_code(initialize(session, request_id=2)), INVALID_REQUEST, msg="twice"
        )

    def test_the_client_is_named_in_a_log_action_without_trusting_its_text(self) -> None:
        """One INITIALIZED line; control characters and length are cleaned."""
        session = make_session()
        params = {
            "protocolVersion": "2025-06-18",
            "clientInfo": {"name": "evil\nname" + "x" * 200, "version": 5},
        }
        logs = [a for a in session.handle(Request(1, "initialize", params)) if isinstance(a, Log)]
        self.assertEqual(len(logs), 1, msg="one line")
        self.assertEqual(logs[0].event, "INITIALIZED", msg="event")
        self.assertNotIn("\n", logs[0].detail, msg="no line break reaches the log")
        self.assertLess(len(logs[0].detail), 200, msg="bounded")
        self.assertIn("protocol=2025-06-18", logs[0].detail, msg="negotiated version")


class BeforeInitializeTests(unittest.TestCase):
    """Only `initialize` and `ping` are served at first."""

    def test_ping_is_served_before_and_after(self) -> None:
        """An empty result."""
        session = make_session()
        self.assertEqual(result_of(list(session.handle(Request(5, "ping", {})))), {}, msg="before")
        initialize(session)
        self.assertEqual(result_of(list(session.handle(Request(6, "ping", {})))), {}, msg="after")

    def test_other_requests_are_refused_and_notifications_ignored(self) -> None:
        """-32600 for a request; silence for a notification."""
        session = make_session()
        self.assertEqual(
            error_code(list(session.handle(Request(5, "tools/list", {})))),
            INVALID_REQUEST,
            msg="request refused",
        )
        self.assertEqual(
            list(session.handle(Notification("notifications/initialized", {}))),
            [],
            msg="notification ignored",
        )


class ServingTests(unittest.TestCase):
    """After `initialize`."""

    def ready(self) -> Session:
        """Return an initialized session."""
        session = make_session()
        initialize(session)
        return session

    def test_tools_list_returns_the_tools_and_refuses_a_cursor(self) -> None:
        """No pagination: a cursor was never issued."""
        session = self.ready()
        self.assertEqual(
            result_of(list(session.handle(Request(2, "tools/list", {})))),
            {"tools": TOOLS},
            msg="the tools",
        )
        self.assertEqual(
            error_code(list(session.handle(Request(3, "tools/list", {"cursor": "x"})))),
            INVALID_PARAMS,
            msg="cursor",
        )

    def test_a_tool_call_is_handed_over_with_its_arguments(self) -> None:
        """The session does not run tools."""
        actions = list(
            self.ready().handle(Request(4, "tools/call", {"name": "wait", "arguments": {"a": 1}}))
        )
        self.assertEqual(actions, [CallTool(4, "wait", {"a": 1})], msg="dispatch")

    def test_missing_arguments_are_an_empty_object_and_bad_ones_are_passed_on(self) -> None:
        """The tool endpoint judges the arguments, not the session."""
        session = self.ready()
        self.assertEqual(
            list(session.handle(Request(4, "tools/call", {"name": "status"}))),
            [CallTool(4, "status", {})],
            msg="absent",
        )
        self.assertEqual(
            list(session.handle(Request(5, "tools/call", {"name": "status", "arguments": [1]}))),
            [CallTool(5, "status", [1])],
            msg="not an object",
        )

    def test_an_unknown_tool_or_a_bad_name_is_invalid_params(self) -> None:
        """-32602."""
        session = self.ready()
        for params in ({"name": "nope"}, {"name": 5}, {}):
            self.assertEqual(
                error_code(list(session.handle(Request(4, "tools/call", params)))),
                INVALID_PARAMS,
                msg=f"{params!r}",
            )

    def test_unknown_methods_are_not_found(self) -> None:
        """Including discovery and the families we do not serve."""
        session = self.ready()
        for method in (
            "server/discover",
            "prompts/list",
            "resources/list",
            "logging/setLevel",
            "x",
        ):
            self.assertEqual(
                error_code(list(session.handle(Request(7, method, {})))),
                METHOD_NOT_FOUND,
                msg=method,
            )

    def test_a_call_before_the_initialized_notification_is_served_and_noted_once(self) -> None:
        """Lenient: the response to `initialize` is enough; one log line says so."""
        session = self.ready()
        first = list(session.handle(Request(4, "tools/call", {"name": "status"})))
        second = list(session.handle(Request(5, "tools/call", {"name": "status"})))
        self.assertTrue(any(isinstance(a, CallTool) for a in first), msg="served")
        self.assertEqual(
            [a.event for a in first if isinstance(a, Log)], ["EARLY_CALL"], msg="noted"
        )
        self.assertEqual([a for a in second if isinstance(a, Log)], [], msg="only once")

    def test_no_note_after_the_initialized_notification(self) -> None:
        """The usual order is silent."""
        session = self.ready()
        session.handle(Notification("notifications/initialized", {}))
        actions = list(session.handle(Request(4, "tools/call", {"name": "status"})))
        self.assertEqual(actions, [CallTool(4, "status", {})], msg="no log action")

    def test_a_cancellation_names_the_request_to_cancel(self) -> None:
        """A readable id becomes a Cancel action; a bad one is dropped."""
        session = self.ready()
        self.assertEqual(
            list(session.handle(Notification("notifications/cancelled", {"requestId": 9}))),
            [Cancel(9)],
            msg="cancel",
        )
        self.assertEqual(
            list(session.handle(Notification("notifications/cancelled", {"requestId": True}))),
            [],
            msg="bool id ignored",
        )
        self.assertEqual(
            list(session.handle(Notification("notifications/cancelled", {}))), [], msg="no id"
        )

    def test_unsolicited_responses_are_logged_not_dispatched(self) -> None:
        """One DROPPED line."""
        actions = list(self.ready().handle(Unsolicited()))
        self.assertEqual(
            [a.event for a in actions if isinstance(a, Log)], ["DROPPED"], msg="logged"
        )
        self.assertFalse(any(isinstance(a, CallTool) for a in actions), msg="never dispatched")


if __name__ == "__main__":
    unittest.main()
