"""The frames waiting to be written: priority frames first, bounded in bytes.

Protocol and control answers go through the priority class so that a pile of large tool results
cannot delay them; after `priority_burst` priority frames in a row one normal frame is served so
the normal class is never starved.
"""

import threading
from collections import deque


class Outbox:
    """A thread-safe two-class queue of encoded frames."""

    def __init__(self, *, max_bytes: int, priority_burst: int) -> None:
        """Keep the byte bound for all waiting frames and the priority burst length."""
        self._max_bytes = max_bytes
        self._priority_burst = priority_burst
        self._priority: deque[bytes] = deque()
        self._normal: deque[bytes] = deque()
        self._bytes = 0
        self._streak = 0
        self._closed = False
        self._ready = threading.Condition()

    def put(self, frame: bytes, *, priority: bool) -> bool:
        """Queue a frame; return False when the outbox is closed or the bound would be exceeded."""
        with self._ready:
            if self._closed or self._bytes + len(frame) > self._max_bytes:
                return False
            (self._priority if priority else self._normal).append(frame)
            self._bytes += len(frame)
            self._ready.notify()
            return True

    def get(self, *, timeout_s: float) -> bytes | None:
        """Take the next frame, waiting up to `timeout_s`; None when none came or all is done."""
        with self._ready:
            if not (self._priority or self._normal) and not self._closed:
                self._ready.wait(timeout_s)
            frame = self._next()
            if frame is not None:
                self._bytes -= len(frame)
            return frame

    def close(self) -> None:
        """Refuse new frames and wake every waiting reader; waiting frames can still be taken."""
        with self._ready:
            self._closed = True
            self._ready.notify_all()

    def _next(self) -> bytes | None:
        """Choose by class and burst; the caller holds the lock."""
        take_normal = bool(self._normal) and (
            not self._priority or self._streak >= self._priority_burst
        )
        if take_normal:
            self._streak = 0
            return self._normal.popleft()
        if self._priority:
            self._streak += 1
            return self._priority.popleft()
        return None
