"""Task modes, provider links and the provider chain of a task type (ARCH L3533, L4211-4213).

This module is pure and names no vendor: a provider is an identifier the settings declare.
"""

import re
from dataclasses import dataclass
from enum import Enum

_NAME = re.compile(r"[a-z][a-z0-9-]{0,31}")


def validate_name(text: str, what: str) -> str:
    """Return `text` when it is a valid task type name or provider id; else raise `ValueError`.

    A name is lowercase letters, digits and hyphens, starts with a letter and has at most 32
    characters. `what` says which kind of name was checked, for the message.
    """
    if _NAME.fullmatch(text) is None:
        message = (
            f"{what} {text!r} is not valid: use lowercase letters, digits and hyphens, "
            "start with a letter, at most 32 characters"
        )
        raise ValueError(message)
    return text


class TaskMode(Enum):
    """Whether a task may write: read tasks cannot, write tasks deliver a result branch."""

    READ = "read"
    WRITE = "write"


@dataclass(frozen=True)
class ProviderLink:
    """One provider in a chain, with the model and effort it overrides for this task only."""

    provider: str
    model: str | None = None
    effort: str | None = None

    def __post_init__(self) -> None:
        """Refuse an id that is not a valid name."""
        validate_name(self.provider, "provider id")


@dataclass(frozen=True)
class HandoffLink:
    """The terminal link: when every execution link refuses, hand the task back as advice."""


Link = ProviderLink | HandoffLink


@dataclass(frozen=True)
class Chain:
    """An ordered chain of responsibility over provider links."""

    links: tuple[Link, ...]

    def __post_init__(self) -> None:
        """Refuse an empty chain, a repeated provider and a handoff that is not the last link."""
        if not self.links:
            message = "the chain is empty"
            raise ValueError(message)
        seen: set[str] = set()
        for position, link in enumerate(self.links):
            if isinstance(link, HandoffLink):
                if position != len(self.links) - 1:
                    message = "a handoff link is allowed only as the last link"
                    raise ValueError(message)
            elif link.provider in seen:
                message = f"provider {link.provider} appears more than once"
                raise ValueError(message)
            else:
                seen.add(link.provider)
