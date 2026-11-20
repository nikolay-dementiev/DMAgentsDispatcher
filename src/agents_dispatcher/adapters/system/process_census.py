"""One look at the macOS process table for the cleanup and the watch (O1 layer 3).

The look is complete only when the list of pids was obtained and the marker of every live process
could be read. A process of another user is not part of the picture: its reads are
refused for good, and no signal could reach it anyway.
"""

import os
from enum import Enum
from typing import Protocol

from agents_dispatcher.application.ports import CensusSnapshot
from agents_dispatcher.domain.attribution import ProcessRow
from agents_dispatcher.domain.process import Absence, ProcessBasics, ProcessKey

MARKER_ENV = "AGENTSD_RUN_ID"


class _Skipped(Enum):
    """Why a process is not in the picture."""

    OUTSIDE = "OUTSIDE"  # gone, another user's, or a zombie: nothing to attribute or signal
    UNREADABLE = "UNREADABLE"  # ours and alive, but its environment could not be read


class CensusTable(Protocol):
    """The reads of the process table that a look needs."""

    def listing(self) -> list[int] | None:
        """Return every visible pid, or None when the table could not be listed."""
        ...

    def basics(self, pid: int) -> ProcessBasics | Absence:
        """Return parent, group, start time and descriptors, or why they are unreadable."""
        ...

    def cwd(self, pid: int) -> str | Absence:
        """Return the working directory, or why it is unreadable."""
        ...

    def open_paths(self, pid: int) -> tuple[str, ...] | Absence:
        """Return the paths of open files, or why they are unreadable."""
        ...

    def environment(self, pid: int) -> dict[str, str] | None:
        """Return the environment at exec, or None when it cannot be read."""
        ...

    def footprint(self, pid: int) -> int | None:
        """Return the physical footprint, or None when the process is gone."""
        ...


class LibprocCensus:
    """A `ProcessCensus` over the libproc table."""

    def __init__(self, table: CensusTable) -> None:
        """Keep the table reader."""
        self._table = table

    def look(self, *, paths: bool) -> CensusSnapshot:
        """Return the live processes of this user; `paths` adds cwd and open files."""
        pids = self._table.listing()
        if pids is None:
            return CensusSnapshot((), complete=False)
        rows: list[ProcessRow] = []
        complete = True
        own = os.getpid()
        for pid in pids:
            if pid == own:
                continue
            row = self._row(pid, paths=paths)
            if row is _Skipped.UNREADABLE:
                complete = False
            elif isinstance(row, ProcessRow):
                rows.append(row)
        return CensusSnapshot(tuple(rows), complete=complete)

    def footprint(self, pid: int) -> int | None:
        """Return the physical footprint in bytes, or None when the process is gone."""
        return self._table.footprint(pid)

    def _row(self, pid: int, *, paths: bool) -> ProcessRow | _Skipped:
        """Read one process: a row, or why there is none.

        Unreadable means a live process whose environment, and so whose marker, could not be read.
        """
        basics = self._table.basics(pid)
        if isinstance(basics, Absence):
            return _Skipped.OUTSIDE
        environment = self._table.environment(pid)
        if environment is None:
            return _Skipped.OUTSIDE if self._ended(pid) else _Skipped.UNREADABLE
        cwd: str | None = None
        files: tuple[str, ...] = ()
        if paths:
            read_cwd = self._table.cwd(pid)
            read_files = self._table.open_paths(pid)
            if read_cwd is Absence.GONE or read_files is Absence.GONE:
                return _Skipped.OUTSIDE
            # Some system daemons of this very user refuse the read of their open files. They are
            # not run processes; they keep their row without paths and do not spoil completeness.
            cwd = None if isinstance(read_cwd, Absence) else read_cwd
            files = () if isinstance(read_files, Absence) else read_files
        return ProcessRow(
            key=ProcessKey(pid, basics.start_time),
            ppid=basics.ppid,
            pgid=basics.pgid,
            marker=environment.get(MARKER_ENV),
            cwd=cwd,
            open_paths=files,
        )

    def _ended(self, pid: int) -> bool:
        """Return whether the process went away since it was read."""
        again = self._table.basics(pid)
        return isinstance(again, Absence)
