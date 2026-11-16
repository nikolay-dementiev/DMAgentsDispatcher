"""Descriptors for the MCP stdio transport: bounded reads, writes that cannot hang forever."""

import os
import select
import time

_DEFAULT_CHUNK_BYTES = 65536
# POSIX guarantees that a pipe accepts this many bytes without blocking once it is writable.
_ATOMIC_WRITE_BYTES = 512


class FdInput:
    """Reads a descriptor in bounded chunks, waiting no longer than the caller allows."""

    def __init__(self, fd: int, chunk_bytes: int = _DEFAULT_CHUNK_BYTES) -> None:
        """Keep the descriptor and the largest chunk one read returns."""
        self._fd = fd
        self._chunk_bytes = chunk_bytes

    def read(self, timeout_s: float) -> bytes | None:
        """Return bytes, b"" at the end of the input, or None when nothing came in time."""
        ready, _, _ = select.select([self._fd], [], [], timeout_s)
        if not ready:
            return None
        return os.read(self._fd, self._chunk_bytes)


class FdOutput:
    """Writes to a descriptor without changing its blocking mode.

    The descriptor may share its open file description with other processes, so it is never made
    non-blocking; instead each piece is written only once the descriptor says it is writable.
    """

    def __init__(self, fd: int) -> None:
        """Keep the descriptor."""
        self._fd = fd

    def write(self, data: bytes, timeout_s: float) -> bool:
        """Write all of `data`; False when the reader stalled for `timeout_s` or went away.

        A False is the failure signal: the transport logs it and ends the server.
        """
        view = memoryview(data)
        deadline = time.monotonic() + timeout_s
        while view:
            left = deadline - time.monotonic()
            try:
                _, writable, _ = select.select([], [self._fd], [], max(0.0, left))
                if not writable:
                    return False
                written = os.write(self._fd, view[:_ATOMIC_WRITE_BYTES])
            except (OSError, ValueError):
                return False
            view = view[written:]
            deadline = time.monotonic() + timeout_s
        return True
