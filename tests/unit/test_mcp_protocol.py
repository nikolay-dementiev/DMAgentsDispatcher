"""Tests of JSON-RPC message classification and the in-flight id registry."""

import threading
import unittest

from agents_dispatcher.interfaces.mcp.protocol import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    Admission,
    InFlightIds,
    Invalid,
    Notification,
    Request,
    Unsolicited,
    classify,
    error_message,
    result_message,
)

STRING_BYTES = 8


def good(**fields: object) -> dict[str, object]:
    """Return a request with `fields` laid over the minimum."""
    message: dict[str, object] = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    message.update(fields)
    return message


def drop(message: dict[str, object], key: str) -> dict[str, object]:
    """Return a copy of `message` without `key`."""
    return {name: value for name, value in message.items() if name != key}


def invalid(message: object) -> Invalid:
    """Classify `message` and return the refusal it earns."""
    outcome = classify(message, STRING_BYTES)
    assert isinstance(outcome, Invalid)  # noqa: S101 - narrows the type
    return outcome


class ClassifyTests(unittest.TestCase):
    """What a decoded frame is."""

    def test_a_request_has_an_id_a_method_and_object_params(self) -> None:
        """The three parts."""
        self.assertEqual(
            classify(good(params={"a": 1}), STRING_BYTES),
            Request(1, "ping", {"a": 1}),
            msg="request with params",
        )
        self.assertEqual(
            classify(good(id="abc"), STRING_BYTES), Request("abc", "ping", {}), msg="string id"
        )

    def test_a_notification_has_no_id(self) -> None:
        """A message without an id key is a notification."""
        self.assertEqual(
            classify(drop(good(method="notifications/initialized"), "id"), STRING_BYTES),
            Notification("notifications/initialized", {}),
            msg="notification",
        )

    def test_a_batch_is_refused_with_a_null_id(self) -> None:
        """Arrays are not supported."""
        refusal = invalid([good()])
        self.assertEqual((refusal.id, refusal.code), (None, INVALID_REQUEST), msg="batch refused")
        self.assertTrue(refusal.respond, msg="and answered")

    def test_anything_else_that_is_not_an_object_is_refused(self) -> None:
        """Strings, numbers, null."""
        for value in ("x", 3, None, True):
            self.assertEqual(invalid(value).code, INVALID_REQUEST, msg=f"{value!r} refused")

    def test_the_version_marker_must_be_exactly_2_0(self) -> None:
        """Missing or wrong marker: refused, with the id when it is readable."""
        for message in (drop(good(), "jsonrpc"), good(jsonrpc="1.0"), good(jsonrpc=2.0)):
            refusal = invalid(message)
            self.assertEqual(refusal.code, INVALID_REQUEST, msg="invalid request")
            self.assertEqual(refusal.id, 1, msg="answered with the id it carried")

    def test_ids_are_strings_or_integers_only(self) -> None:
        """Bool, float, null, list, object and over-long strings are refused with a null id."""
        for bad in (True, 1.5, None, [1], {"a": 1}, "x" * (STRING_BYTES + 1), "é" * STRING_BYTES):
            refusal = invalid(good(id=bad))
            self.assertEqual(refusal.code, INVALID_REQUEST, msg=f"{bad!r} refused")
            self.assertIsNone(refusal.id, msg=f"{bad!r} cannot be echoed")
        self.assertIsInstance(classify(good(id="x" * STRING_BYTES), STRING_BYTES), Request)
        self.assertIsInstance(classify(good(id=0), STRING_BYTES), Request, msg="zero is an id")

    def test_a_malformed_request_is_answered_with_its_id(self) -> None:
        """A readable id is echoed."""
        refusal = invalid(good(method=5))
        self.assertEqual((refusal.id, refusal.code), (1, INVALID_REQUEST), msg="method not text")
        refusal = invalid(drop(good(), "method"))
        self.assertEqual((refusal.id, refusal.code), (1, INVALID_REQUEST), msg="no method")

    def test_params_must_be_an_object_when_present(self) -> None:
        """-32602 for a request; a notification is dropped without an answer."""
        refusal = invalid(good(params=[1]))
        self.assertEqual((refusal.id, refusal.code), (1, INVALID_PARAMS), msg="request refused")
        silent = invalid(drop(good(params="x"), "id"))
        self.assertFalse(silent.respond, msg="a malformed notification gets no response")

    def test_a_response_shaped_message_is_never_a_request(self) -> None:
        """Results and errors the server did not ask for are set aside."""
        self.assertIsInstance(
            classify({"jsonrpc": "2.0", "id": 1, "result": {}}, STRING_BYTES),
            Unsolicited,
            msg="result",
        )
        self.assertIsInstance(
            classify({"jsonrpc": "2.0", "id": 1, "error": {"code": 1}}, STRING_BYTES),
            Unsolicited,
            msg="error",
        )


class BuilderTests(unittest.TestCase):
    """The messages the server writes."""

    def test_a_result_and_an_error_carry_the_id(self) -> None:
        """Shapes."""
        self.assertEqual(
            result_message(7, {"ok": True}),
            {"jsonrpc": "2.0", "id": 7, "result": {"ok": True}},
            msg="result",
        )
        self.assertEqual(
            error_message(None, INVALID_REQUEST, "bad"),
            {"jsonrpc": "2.0", "id": None, "error": {"code": INVALID_REQUEST, "message": "bad"}},
            msg="error with a null id",
        )


class InFlightIdsTests(unittest.TestCase):
    """The ids of requests that are admitted and not yet answered."""

    def test_an_id_in_flight_cannot_be_used_again_until_it_is_released(self) -> None:
        """Duplicate refused; after release, fine."""
        ids = InFlightIds(maximum=4)
        self.assertEqual(ids.begin(1), Admission.ADMITTED, msg="first use")
        self.assertEqual(ids.begin(1), Admission.DUPLICATE, msg="second use while in flight")
        ids.finish(1)
        self.assertEqual(ids.begin(1), Admission.ADMITTED, msg="free again after the answer")

    def test_the_integer_one_and_the_text_one_are_different_ids(self) -> None:
        """JSON-RPC ids compare by type and value."""
        ids = InFlightIds(maximum=4)
        self.assertEqual(ids.begin(1), Admission.ADMITTED, msg="integer")
        self.assertEqual(ids.begin("1"), Admission.ADMITTED, msg="text")

    def test_the_registry_is_bounded(self) -> None:
        """One more than the maximum is FULL, and releasing makes room."""
        ids = InFlightIds(maximum=2)
        ids.begin(1)
        ids.begin(2)
        self.assertEqual(ids.begin(3), Admission.FULL, msg="over the bound")
        ids.finish(1)
        self.assertEqual(ids.begin(3), Admission.ADMITTED, msg="room again")

    def test_cancelling_marks_only_an_id_in_flight(self) -> None:
        """Unknown and finished ids are ignored; the flag is readable from another thread."""
        ids = InFlightIds(maximum=4)
        self.assertFalse(ids.cancel(9), msg="unknown id ignored")
        ids.begin(5)
        self.assertFalse(ids.is_cancelled(5), msg="not cancelled yet")
        seen: list[bool] = []
        worker = threading.Thread(target=lambda: (ids.cancel(5), seen.append(ids.is_cancelled(5))))
        worker.start()
        worker.join()
        self.assertEqual(seen, [True], msg="cancel is visible")
        ids.finish(5)
        self.assertFalse(ids.cancel(5), msg="finished id ignored")
        self.assertFalse(ids.is_cancelled(5), msg="and forgotten")


if __name__ == "__main__":
    unittest.main()
