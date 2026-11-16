"""A checker for the small subset of JSON Schema the tools use.

Supported: type, properties, required, additionalProperties (false), enum, items, minimum,
maximum, exclusiveMinimum, minLength, maxLength, pattern, oneOf. No runtime dependency is allowed
in this project, and the tools need nothing more. The first violation found is returned.
"""

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Violation:
    """Where a value breaks a schema, which keyword it breaks, and a sentence about it."""

    path: str
    keyword: str
    message: str


def check(schema: Mapping[str, object], value: object) -> Violation | None:
    """Return the first violation of `schema` by `value`, or None when it conforms."""
    return _check(schema, value, "")


def _check(schema: Mapping[str, object], value: object, path: str) -> Violation | None:
    """Check one value against one schema node."""
    for keyword, rule in _KEYWORDS:
        if keyword in schema:
            found = rule(schema, value, path)
            if found is not None:
                return found
    if isinstance(value, dict):
        return _check_object(schema, value, path)
    if isinstance(value, list):
        return _check_array(schema, value, path)
    if isinstance(value, str):
        return _check_string(schema, value, path)
    if _is_number(value):
        return _check_number(schema, float(value), path)  # type: ignore[arg-type]
    return None


def _label(path: str) -> str:
    """Return how a path reads in a message."""
    return repr(path) if path else "the value"


def _is_number(value: object) -> bool:
    """Return whether `value` is a JSON number (a bool is not)."""
    return isinstance(value, int | float) and not isinstance(value, bool)


def _bound(schema: Mapping[str, object], key: str) -> float | None:
    """Return a numeric bound of the schema, or None when it has none."""
    bound = schema.get(key)
    if isinstance(bound, int | float) and not isinstance(bound, bool):
        return float(bound)
    return None


def _length(schema: Mapping[str, object], key: str) -> int | None:
    """Return a length bound of the schema, or None when it has none."""
    bound = schema.get(key)
    return bound if isinstance(bound, int) and not isinstance(bound, bool) else None


_TYPES: dict[str, Callable[[object], bool]] = {
    "object": lambda value: isinstance(value, dict),
    "array": lambda value: isinstance(value, list),
    "string": lambda value: isinstance(value, str),
    "boolean": lambda value: isinstance(value, bool),
    "null": lambda value: value is None,
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": _is_number,
}


def _check_type(schema: Mapping[str, object], value: object, path: str) -> Violation | None:
    """Check the `type` keyword."""
    expected = str(schema["type"])
    if _TYPES[expected](value):
        return None
    return Violation(path, "type", f"{_label(path)} must be of type {expected}")


def _check_enum(schema: Mapping[str, object], value: object, path: str) -> Violation | None:
    """Check the `enum` keyword."""
    allowed = schema["enum"]
    if isinstance(allowed, list) and value in allowed:
        return None
    return Violation(path, "enum", f"{_label(path)} must be one of {allowed}")


def _check_one_of(schema: Mapping[str, object], value: object, path: str) -> Violation | None:
    """Check the `oneOf` keyword: exactly one alternative must accept the value."""
    alternatives = schema["oneOf"]
    if not isinstance(alternatives, list):
        return None
    matches = sum(1 for item in alternatives if _check(item, value, path) is None)
    if matches == 1:
        return None
    return Violation(path, "oneOf", f"{_label(path)} must match exactly one of the alternatives")


_KEYWORDS = (("type", _check_type), ("enum", _check_enum), ("oneOf", _check_one_of))


def _check_object(
    schema: Mapping[str, object], value: dict[str, object], path: str
) -> Violation | None:
    """Check required, properties and additionalProperties."""
    required = schema.get("required", [])
    if isinstance(required, list):
        for name in required:
            if name not in value:
                return Violation(_join(path, str(name)), "required", f"{name!r} is required")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict):
        return None
    for name, item in value.items():
        child = _join(path, name)
        if name in properties:
            found = _check(properties[name], item, child)
            if found is not None:
                return found
        elif schema.get("additionalProperties") is False:
            return Violation(child, "additionalProperties", f"unknown member {name!r}")
    return None


def _check_array(schema: Mapping[str, object], value: list[object], path: str) -> Violation | None:
    """Check items."""
    items = schema.get("items")
    if not isinstance(items, dict):
        return None
    for index, item in enumerate(value):
        found = _check(items, item, f"{path}[{index}]")
        if found is not None:
            return found
    return None


def _check_string(schema: Mapping[str, object], value: str, path: str) -> Violation | None:
    """Check minLength, maxLength and pattern."""
    least = _length(schema, "minLength")
    if least is not None and len(value) < least:
        return Violation(path, "minLength", f"{_label(path)} needs at least {least} characters")
    most = _length(schema, "maxLength")
    if most is not None and len(value) > most:
        return Violation(path, "maxLength", f"{_label(path)} allows at most {most} characters")
    pattern = schema.get("pattern")
    if isinstance(pattern, str) and re.search(pattern, value) is None:
        return Violation(path, "pattern", f"{_label(path)} must match {pattern}")
    return None


def _check_number(schema: Mapping[str, object], value: float, path: str) -> Violation | None:
    """Check minimum, maximum and exclusiveMinimum."""
    least = _bound(schema, "minimum")
    if least is not None and value < least:
        return Violation(path, "minimum", f"{_label(path)} must be at least {schema['minimum']}")
    most = _bound(schema, "maximum")
    if most is not None and value > most:
        return Violation(path, "maximum", f"{_label(path)} must be at most {schema['maximum']}")
    above = _bound(schema, "exclusiveMinimum")
    if above is not None and value <= above:
        text = f"{_label(path)} must be above {schema['exclusiveMinimum']}"
        return Violation(path, "exclusiveMinimum", text)
    return None


def _join(path: str, name: str) -> str:
    """Extend a path by a member name."""
    return f"{path}.{name}" if path else name
