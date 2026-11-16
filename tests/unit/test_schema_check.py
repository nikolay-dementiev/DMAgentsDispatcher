"""Tests of the small JSON Schema subset checker."""

import unittest
from collections.abc import Mapping

from agents_dispatcher.application.schema_check import Violation, check

OBJECT: dict[str, object] = {
    "type": "object",
    "properties": {
        "name": {"type": "string", "minLength": 1, "maxLength": 5},
        "count": {"type": "integer", "minimum": 0, "maximum": 10},
        "ratio": {"type": "number", "exclusiveMinimum": 0},
        "mode": {"enum": ["a", "b"]},
        "tags": {"type": "array", "items": {"type": "string"}},
        "flag": {"type": "boolean"},
        "none": {"type": "null"},
        "slug": {"type": "string", "pattern": "^[a-z]+$"},
    },
    "required": ["name"],
    "additionalProperties": False,
}


def violation(value: object, schema: Mapping[str, object] = OBJECT) -> Violation:
    """Return the violation, failing when the value is valid."""
    found = check(schema, value)
    assert found is not None, f"{value!r} was accepted"  # noqa: S101 - the test's own claim
    return found


class CheckTests(unittest.TestCase):
    """Each keyword of the subset, accepted at the bound and refused one past it."""

    def test_a_valid_value_has_no_violation(self) -> None:
        """Everything in range."""
        value = {"name": "abc", "count": 3, "ratio": 0.5, "mode": "a", "tags": ["x"], "flag": True}
        self.assertIsNone(check(OBJECT, value), msg="valid")

    def test_types_are_strict(self) -> None:
        """A bool is not an integer; an integer is a number; a float is not an integer."""
        self.assertEqual(violation({"name": "a", "count": True}).keyword, "type", msg="bool")
        self.assertEqual(violation({"name": "a", "count": 1.5}).keyword, "type", msg="float")
        self.assertIsNone(check(OBJECT, {"name": "a", "ratio": 2}), msg="int is a number")
        self.assertEqual(violation({"name": "a", "ratio": True}).keyword, "type", msg="bool no")
        self.assertEqual(violation("x").keyword, "type", msg="string for object")
        self.assertEqual(violation({"name": 5}).keyword, "type", msg="number for string")
        self.assertEqual(violation({"name": "a", "none": 0}).keyword, "type", msg="null")

    def test_required_and_additional_properties(self) -> None:
        """A missing member and an unknown member are named."""
        missing = violation({})
        self.assertEqual((missing.keyword, missing.path), ("required", "name"), msg="missing")
        extra = violation({"name": "a", "other": 1})
        self.assertEqual(
            (extra.keyword, extra.path), ("additionalProperties", "other"), msg="extra"
        )

    def test_string_length_bounds(self) -> None:
        """Counted in characters."""
        self.assertIsNone(check(OBJECT, {"name": "abcde"}), msg="max length passes")
        self.assertEqual(violation({"name": "abcdef"}).keyword, "maxLength", msg="one over")
        self.assertEqual(violation({"name": ""}).keyword, "minLength", msg="empty")

    def test_numeric_bounds(self) -> None:
        """Inclusive and exclusive."""
        self.assertIsNone(check(OBJECT, {"name": "a", "count": 10}), msg="maximum passes")
        self.assertEqual(violation({"name": "a", "count": 11}).keyword, "maximum", msg="over")
        self.assertEqual(violation({"name": "a", "count": -1}).keyword, "minimum", msg="under")
        self.assertEqual(
            violation({"name": "a", "ratio": 0}).keyword, "exclusiveMinimum", msg="zero refused"
        )

    def test_enum_pattern_and_items(self) -> None:
        """Members of a closed set, a pattern, array elements."""
        self.assertEqual(violation({"name": "a", "mode": "c"}).keyword, "enum", msg="enum")
        self.assertEqual(violation({"name": "a", "slug": "Ab"}).keyword, "pattern", msg="pattern")
        bad = violation({"name": "a", "tags": ["x", 2]})
        self.assertEqual((bad.keyword, bad.path), ("type", "tags[1]"), msg="items")

    def test_a_pattern_is_a_search_not_a_full_match(self) -> None:
        """JSON Schema patterns are unanchored unless they say otherwise."""
        schema: dict[str, object] = {"type": "string", "pattern": "ab"}
        self.assertIsNone(check(schema, "xaby"), msg="found inside")
        self.assertEqual(violation("xyz", schema).keyword, "pattern", msg="not found")

    def test_one_of_needs_exactly_one_match(self) -> None:
        """Neither and both are refused."""
        schema: dict[str, object] = {
            "oneOf": [
                {"type": "object", "required": ["a"]},
                {"type": "object", "required": ["b"]},
            ]
        }
        self.assertIsNone(check(schema, {"a": 1}), msg="first only")
        self.assertEqual(violation({}, schema).keyword, "oneOf", msg="none")
        self.assertEqual(violation({"a": 1, "b": 2}, schema).keyword, "oneOf", msg="both")

    def test_a_violation_has_a_readable_message(self) -> None:
        """Path and rule in the text."""
        found = violation({"name": "a", "count": 11})
        self.assertIn("count", found.message, msg="names the member")
        self.assertIn("10", found.message, msg="names the bound")


if __name__ == "__main__":
    unittest.main()
