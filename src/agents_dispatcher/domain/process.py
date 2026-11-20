"""What the process table can say about a process, or why it cannot."""

from dataclasses import dataclass
from enum import Enum


class Absence(Enum):
    """Why a process could not be read; the two causes are never merged."""

    GONE = "GONE"  # the process ended, or never existed
    DENIED = "DENIED"  # it exists but belongs to another user, so it cannot be read or signalled


@dataclass(frozen=True)
class ProcessKey:
    """The identity of one process: a pid is reused, so the start time is part of it."""

    pid: int
    start_time: float


@dataclass(frozen=True)
class ProcessBasics:
    """The facts of one live process that identity and attribution need."""

    pid: int
    ppid: int
    pgid: int
    start_time: float
    descriptor_count: int
