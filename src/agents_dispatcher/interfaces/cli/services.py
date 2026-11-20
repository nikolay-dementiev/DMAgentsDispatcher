"""The ports only the agentsd command line needs: version and interrupt flag."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Protocol


class VersionSource(Protocol):
    """Reports the installed version of the distribution, if there is one."""

    def installed_version(self) -> str | None:
        """Return the installed version, or None when the metadata is missing."""
        ...


class InterruptFlag(Protocol):
    """Tells whether the user interrupted the command."""

    def raised(self) -> bool:
        """Return True once SIGINT or SIGTERM arrived."""
        ...

    def count(self) -> int:
        """Return how many of these signals arrived in this scope."""
        ...


InterruptScope = Callable[[], AbstractContextManager[InterruptFlag]]
