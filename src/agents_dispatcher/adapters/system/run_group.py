"""Decides whether a stored process group is still the one a run started."""

import os

from agents_dispatcher.adapters.system.seatbelt_confinement import ProcessTable
from agents_dispatcher.domain.process import Absence

START_TIME_TOLERANCE_S = 1.0
SYSTEM_GROUP_MAX = 1  # pgid 0 means the caller's group and 1 is launchd: never a run's group


def group_is_ours(table: ProcessTable, pgid: int, pg_start_time: float | None) -> bool:
    """Return whether the stored group is the one the run started.

    A living leader must have the stored start time. A dead leader whose group still has members
    means the group id is still held by the run's own processes. A leader that cannot be read at
    all belongs to someone else: it is not ours.
    """
    leader = table.basics(pgid)
    if leader is Absence.DENIED:
        return False
    if isinstance(leader, Absence):
        return bool(table.group_members(pgid))
    return abs(leader.start_time - (pg_start_time or 0.0)) < START_TIME_TOLERANCE_S


def verified_group(table: ProcessTable, pgid: int | None, pg_start_time: float | None) -> int:
    """Return the stored group when it is provably the run's, else -1 (no group matches it).

    A damaged row naming pgid 0, pgid 1 or our own group must never attribute a whole group.
    """
    if pgid is None or pg_start_time is None:
        return -1
    if pgid <= SYSTEM_GROUP_MAX or pgid == os.getpgrp():
        return -1
    return pgid if group_is_ours(table, pgid, pg_start_time) else -1
