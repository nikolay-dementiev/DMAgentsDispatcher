"""Narrowing helper for tests, so that no bare `assert` is needed to satisfy the type checker."""


def present[T](value: T | None) -> T:
    """Return `value`, or fail the test when it is None."""
    if value is None:
        message = "expected a value, got None"
        raise AssertionError(message)
    return value
