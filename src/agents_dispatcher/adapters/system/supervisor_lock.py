"""A file lock that tells a start-up whether another supervisor is alive.

Every live supervisor holds a shared lock on one file in the state folder. A start-up may sweep
the runs recorded as unfinished only when it can take the exclusive lock, which means no other
supervisor holds one. The kernel drops the lock when a supervisor dies, so a crashed supervisor
never blocks recovery and a live one is never mistaken for dead.
"""

import fcntl
import os

LOCK_MODE = 0o600


class SupervisorLock:
    """The lock of one supervisor process; the descriptor stays open for the process lifetime."""

    def __init__(self, path: str) -> None:
        """Open (or create) the lock file; nothing is locked yet."""
        self._descriptor = os.open(path, os.O_CREAT | os.O_RDWR, LOCK_MODE)

    def try_exclusive(self) -> bool:
        """Return whether no other supervisor is alive; if so, the exclusive lock is held."""
        try:
            fcntl.flock(self._descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def stay_alive(self) -> None:
        """Hold the shared lock from now on, so later start-ups see this supervisor."""
        fcntl.flock(self._descriptor, fcntl.LOCK_SH)
