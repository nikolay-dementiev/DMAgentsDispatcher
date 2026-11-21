"""Reads a process's start time through `basics`, the one reading that names why it failed."""

from typing import Protocol

from agents_dispatcher.domain.process import Absence, ProcessBasics


class BasicsTable(Protocol):
    """The one read of the process table this helper needs."""

    def basics(self, pid: int) -> ProcessBasics | Absence:
        """Return parent, group, start time and descriptors, or why they are unreadable."""
        ...


def start_time_of(table: BasicsTable, pid: int) -> float | None:
    """Return the start time of a readable process, or None for any other reading."""
    reading = table.basics(pid)
    return reading.start_time if isinstance(reading, ProcessBasics) else None
