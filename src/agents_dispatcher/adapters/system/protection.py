"""Processes that no cleanup may ever signal: the system's own and this process's lineage."""

import os
from typing import Protocol

from agents_dispatcher.domain.process import Absence, ProcessBasics

SYSTEM_PID_MAX = 1  # pid 0 is the kernel group and pid 1 is launchd


class ParentTable(Protocol):
    """The one read the lineage needs."""

    def basics(self, pid: int) -> ProcessBasics | Absence:
        """Return the facts of the process, or why they are unreadable."""
        ...


def never_signalled(table: ParentTable) -> frozenset[int]:
    """Return pid 0 and 1, this process and every ancestor of it."""
    pids = {0, SYSTEM_PID_MAX}
    pid = os.getpid()
    while pid > SYSTEM_PID_MAX and pid not in pids:
        pids.add(pid)
        basics = table.basics(pid)
        if not isinstance(basics, ProcessBasics):
            break
        pid = basics.ppid
    return frozenset(pids)
