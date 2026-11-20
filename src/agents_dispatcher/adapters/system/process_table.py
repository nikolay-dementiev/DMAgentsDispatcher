"""Reads the macOS process table through libproc and sysctl (digest section 1).

Everything is read through `ctypes`, so no dependency is added. A process that disappears
between the listing and the query is reported as absent (`None` or an empty list), never as an
error: the table is a live view and processes end at any time.
"""

import ctypes
import ctypes.util
import errno
import struct

from agents_dispatcher.domain.process import Absence, ProcessBasics

PROC_ALL_PIDS = 1
PROC_PGRP_ONLY = 2
PROC_PIDTBSDINFO = 3
PROC_PIDLISTFDS = 1
PROC_PIDFDVNODEPATHINFO = 2
PROC_PIDVNODEPATHINFO = 9
PROX_FDTYPE_VNODE = 1
# Offsets and sizes below were measured on macOS 27.2.1 (arm64) against `os.getppid()`, a child's
# real cwd and a held file; see the slice-1 probes of the O1 plan.
_BSDINFO_PPID_OFFSET = 16
_BSDINFO_NFILES_OFFSET = 96
_BSDINFO_PGID_OFFSET = 100
_VNODEPATHINFO_SIZE = 2352
_VNODEPATHINFO_CWD_OFFSET = 152
_FDVNODEPATHINFO_SIZE = 1200
_FDVNODEPATHINFO_PATH_OFFSET = 176
_PATH_SIZE = 1024
_FDINFO_SIZE = 8
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


def _absence(error: int) -> Absence:
    """Map the errno of a failed read: only ESRCH means gone; anything else is not provably so."""
    return Absence.GONE if error == errno.ESRCH else Absence.DENIED


def _path_at(raw: bytes, offset: int) -> str:
    """Decode the NUL-terminated path that starts at `offset`."""
    return raw[offset : offset + _PATH_SIZE].split(b"\0", 1)[0].decode("utf-8", "replace")


class LibprocProcessTable:
    """A `ProcessTable` over `libproc` and `sysctl(KERN_PROCARGS2)`."""

    def __init__(self) -> None:
        """Bind the system calls."""
        self._proc = _load("proc")
        self._c = _load("c")

    def all_pids(self) -> list[int]:
        """Return every pid visible to this user."""
        return self._list_pids(PROC_ALL_PIDS, 0)

    def listing(self) -> list[int] | None:
        """Return every pid visible to this user, or None when the table could not be listed."""
        needed = self._proc.proc_listpids(PROC_ALL_PIDS, 0, None, 0)
        if needed <= 0:
            return None
        capacity = needed + 64 * PID_SIZE
        buffer = (ctypes.c_int * (capacity // PID_SIZE))()
        written = self._proc.proc_listpids(PROC_ALL_PIDS, 0, buffer, capacity)
        if written <= 0:
            return None
        return [pid for pid in buffer[: written // PID_SIZE] if pid > 0]

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

    def basics(self, pid: int) -> ProcessBasics | Absence:
        """Return parent, group, start time and descriptor count, or why they are unreadable.

        A zombie (ended, not yet reaped) answers ESRCH on macOS, so it reads as `GONE`: it holds
        nothing and cannot be signalled.
        """
        buffer = ctypes.create_string_buffer(_BSDINFO_SIZE)
        written = self._proc.proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, buffer, _BSDINFO_SIZE)
        if written != _BSDINFO_SIZE:
            return _absence(ctypes.get_errno())
        raw = buffer.raw
        (ppid,) = struct.unpack_from("<I", raw, _BSDINFO_PPID_OFFSET)
        (nfiles,) = struct.unpack_from("<I", raw, _BSDINFO_NFILES_OFFSET)
        (pgid,) = struct.unpack_from("<I", raw, _BSDINFO_PGID_OFFSET)
        seconds, microseconds = struct.unpack_from("<QQ", raw, _BSDINFO_START_SEC_OFFSET)
        return ProcessBasics(
            pid=pid,
            ppid=ppid,
            pgid=pgid,
            start_time=float(seconds + microseconds / _MICROSECONDS),
            descriptor_count=nfiles,
        )

    def cwd(self, pid: int) -> str | Absence:
        """Return the real path of the working directory, or why it is unreadable."""
        buffer = ctypes.create_string_buffer(_VNODEPATHINFO_SIZE)
        written = self._proc.proc_pidinfo(
            pid, PROC_PIDVNODEPATHINFO, 0, buffer, _VNODEPATHINFO_SIZE
        )
        if written != _VNODEPATHINFO_SIZE:
            return _absence(ctypes.get_errno())
        return _path_at(buffer.raw, _VNODEPATHINFO_CWD_OFFSET)

    def open_paths(self, pid: int) -> tuple[str, ...] | Absence:
        """Return the real paths of the files the process holds open, or why they are unreadable.

        Only descriptors that name a vnode have a path; sockets and pipes do not appear.
        """
        needed = self._proc.proc_pidinfo(pid, PROC_PIDLISTFDS, 0, None, 0)
        if needed <= 0:
            return _absence(ctypes.get_errno())
        capacity = needed + 16 * _FDINFO_SIZE
        listing = ctypes.create_string_buffer(capacity)
        written = self._proc.proc_pidinfo(pid, PROC_PIDLISTFDS, 0, listing, capacity)
        if written <= 0:
            return _absence(ctypes.get_errno())
        paths: list[str] = []
        for offset in range(0, written - _FDINFO_SIZE + 1, _FDINFO_SIZE):
            descriptor, kind = struct.unpack_from("<iI", listing.raw, offset)
            if kind != PROX_FDTYPE_VNODE:
                continue
            path = self._descriptor_path(pid, descriptor)
            if isinstance(path, Absence):
                if path is Absence.DENIED:
                    return path
                continue  # the descriptor was closed between the listing and the query
            paths.append(path)
        return tuple(paths)

    def _descriptor_path(self, pid: int, descriptor: int) -> str | Absence:
        """Return the path behind one vnode descriptor."""
        buffer = ctypes.create_string_buffer(_FDVNODEPATHINFO_SIZE)
        written = self._proc.proc_pidfdinfo(
            pid, descriptor, PROC_PIDFDVNODEPATHINFO, buffer, _FDVNODEPATHINFO_SIZE
        )
        if written != _FDVNODEPATHINFO_SIZE:
            return _absence(ctypes.get_errno())
        return _path_at(buffer.raw, _FDVNODEPATHINFO_PATH_OFFSET)

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
