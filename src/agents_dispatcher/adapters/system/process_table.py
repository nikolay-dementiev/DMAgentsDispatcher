"""Reads the macOS process table through libproc and sysctl (digest section 1).

Everything is read through `ctypes`, so no dependency is added. A process that disappears
between the listing and the query is reported as absent (`None` or an empty list), never as an
error: the table is a live view and processes end at any time.
"""

import ctypes
import ctypes.util
import struct

PROC_ALL_PIDS = 1
PROC_PGRP_ONLY = 2
PROC_PIDTBSDINFO = 3
RUSAGE_INFO_V0 = 0
CTL_KERN = 1
KERN_PROCARGS2 = 49
PID_SIZE = 4
_BSDINFO_SIZE = 136
_BSDINFO_START_SEC_OFFSET = 120
_RUSAGE_V0_SIZE = 136
_RUSAGE_PHYS_FOOTPRINT_OFFSET = 72
_MICROSECONDS = 1_000_000.0
_ARGC_SIZE = 4


def _load(name: str) -> ctypes.CDLL:
    """Load a system library or fail with the library's name."""
    path = ctypes.util.find_library(name)
    if path is None:
        message = f"the system library {name!r} was not found"
        raise OSError(message)
    return ctypes.CDLL(path, use_errno=True)


class LibprocProcessTable:
    """A `ProcessTable` over `libproc` and `sysctl(KERN_PROCARGS2)`."""

    def __init__(self) -> None:
        """Bind the system calls."""
        self._proc = _load("proc")
        self._c = _load("c")

    def all_pids(self) -> list[int]:
        """Return every pid visible to this user."""
        return self._list_pids(PROC_ALL_PIDS, 0)

    def group_members(self, pgid: int) -> list[int]:
        """Return the pids in the process group; empty when the group no longer exists."""
        return self._list_pids(PROC_PGRP_ONLY, pgid)

    def start_time(self, pid: int) -> float | None:
        """Return the wall-clock start time of the process, or None when it is gone."""
        buffer = ctypes.create_string_buffer(_BSDINFO_SIZE)
        written = self._proc.proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, buffer, _BSDINFO_SIZE)
        if written != _BSDINFO_SIZE:
            return None
        seconds, microseconds = struct.unpack_from("<QQ", buffer.raw, _BSDINFO_START_SEC_OFFSET)
        return float(seconds + microseconds / _MICROSECONDS)

    def footprint(self, pid: int) -> int | None:
        """Return `ri_phys_footprint` in bytes, or None when the process is gone."""
        buffer = ctypes.create_string_buffer(_RUSAGE_V0_SIZE)
        status = self._proc.proc_pid_rusage(pid, RUSAGE_INFO_V0, buffer)
        if status != 0:
            return None
        (value,) = struct.unpack_from("<Q", buffer.raw, _RUSAGE_PHYS_FOOTPRINT_OFFSET)
        return int(value)

    def environment(self, pid: int) -> dict[str, str] | None:
        """Return the environment as it was at exec, or None when it cannot be read.

        Processes of protected Apple binaries yield an empty mapping (digest section 1).
        """
        raw = self._procargs(pid)
        if raw is None or len(raw) < _ARGC_SIZE:
            return None
        (argc,) = struct.unpack_from("=i", raw, 0)
        parts = raw[_ARGC_SIZE:].split(b"\0")
        # parts: exec path, padding empties, argv (argc entries), then the environment.
        index = 1
        while index < len(parts) and parts[index] == b"":
            index += 1
        environment: dict[str, str] = {}
        for entry in parts[index + argc :]:
            if not entry:
                break
            key, separator, value = entry.decode("utf-8", "replace").partition("=")
            if separator:
                environment[key] = value
        return environment

    def _list_pids(self, kind: int, info: int) -> list[int]:
        """Call `proc_listpids` with a buffer sized by a first query."""
        needed = self._proc.proc_listpids(kind, info, None, 0)
        if needed <= 0:
            return []
        capacity = needed + 64 * PID_SIZE
        buffer = (ctypes.c_int * (capacity // PID_SIZE))()
        written = self._proc.proc_listpids(kind, info, buffer, capacity)
        if written <= 0:
            return []
        return [pid for pid in buffer[: written // PID_SIZE] if pid > 0]

    def _procargs(self, pid: int) -> bytes | None:
        """Read the raw `KERN_PROCARGS2` block, or None on any failure."""
        name = (ctypes.c_int * 3)(CTL_KERN, KERN_PROCARGS2, pid)
        size = ctypes.c_size_t(0)
        if self._c.sysctl(name, 3, None, ctypes.byref(size), None, 0) != 0 or size.value == 0:
            return None
        buffer = ctypes.create_string_buffer(size.value)
        if self._c.sysctl(name, 3, buffer, ctypes.byref(size), None, 0) != 0:
            return None
        return buffer.raw[: size.value]
