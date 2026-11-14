"""One exclusive file lock per run tells every other process whether the run has a live owner.

The supervisor of a run holds an exclusive `flock` on `<control>/locks/<run id>.lock` for as long
as it lives. The kernel drops the lock when the process dies, so a crashed supervisor never
blocks recovery and a live one is never mistaken for dead. A probe takes a shared lock without
waiting: if that succeeds, nobody holds the exclusive one. Descriptors are not inherited by
child processes, so a worker can never keep a dead supervisor's lock alive.
"""

import fcntl
import os
import time
from pathlib import Path

from agents_dispatcher.domain.run import RunId

FOLDER_MODE = 0o700
FILE_MODE = 0o600
# A status probe holds a shared lock for a moment; `hold` waits this long for it to let go.
HOLD_RETRY_S = 1.0
HOLD_POLL_S = 0.01


class OwnerLockError(Exception):
    """The run already has a live owner, or its lock file cannot be used."""


class RunOwnership:
    """The exclusive lock of one run; release it or let the process end."""

    def __init__(self, descriptor: int) -> None:
        """Keep the descriptor that holds the lock."""
        self._descriptor: int | None = descriptor

    def release(self) -> None:
        """Drop the lock; a second release does nothing."""
        if self._descriptor is not None:
            os.close(self._descriptor)
            self._descriptor = None


class FileRunOwners:
    """Takes and probes the per-run owner locks under one control folder."""

    def __init__(self, control_dir: str, hold_retry_s: float = HOLD_RETRY_S) -> None:
        """Keep the control folder and the retry bound; nothing is created until a lock is taken."""
        self._hold_retry_s = hold_retry_s
        self._locks = Path(control_dir) / "locks"
        self._root = Path(control_dir)

    def hold(self, run_id: RunId) -> RunOwnership:
        """Take the exclusive lock of the run; raise `OwnerLockError` when someone holds it."""
        try:
            self._root.mkdir(mode=FOLDER_MODE, parents=True, exist_ok=True)
            self._root.chmod(FOLDER_MODE)
            self._locks.mkdir(mode=FOLDER_MODE, exist_ok=True)
            self._locks.chmod(FOLDER_MODE)
            descriptor = os.open(self._path(run_id), os.O_CREAT | os.O_RDWR, FILE_MODE)
        except OSError as error:
            message = f"cannot open the owner lock of run {run_id.value}: {error}"
            raise OwnerLockError(message) from error
        try:
            self._lock_exclusively(descriptor)
        except BlockingIOError as error:
            os.close(descriptor)
            message = f"run {run_id.value} already has a live owner"
            raise OwnerLockError(message) from error
        except OSError as error:
            os.close(descriptor)
            message = f"cannot lock the owner lock of run {run_id.value}: {error}"
            raise OwnerLockError(message) from error
        return RunOwnership(descriptor)

    def _lock_exclusively(self, descriptor: int) -> None:
        """Take the lock without blocking, retrying while a probe or owner holds it, for a bound."""
        deadline = time.monotonic() + self._hold_retry_s
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(HOLD_POLL_S)
            else:
                return

    def owner_alive(self, run_id: RunId) -> bool:
        """Return whether some process holds the run's lock; a missing lock file means no."""
        try:
            descriptor = os.open(self._path(run_id), os.O_RDONLY)
        except FileNotFoundError:
            return False
        except OSError as error:
            message = f"cannot read the owner lock of run {run_id.value}: {error}"
            raise OwnerLockError(message) from error
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        except OSError as error:
            message = f"cannot probe the owner lock of run {run_id.value}: {error}"
            raise OwnerLockError(message) from error
        finally:
            os.close(descriptor)
        return False

    def _path(self, run_id: RunId) -> str:
        """Return the lock file of the run; the id is a validated UUID, so it is one name."""
        return str(self._locks / f"{run_id.value}.lock")
