"""The text of an epoch time, for notes and log lines."""

from datetime import UTC, datetime


def iso_utc(epoch_s: float) -> str:
    """Return the time as whole seconds in UTC, e.g. 2026-11-21T10:00:00Z."""
    return datetime.fromtimestamp(epoch_s, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
