"""One exclusive file lock per run tells every other process whether the run has a live owner.

The supervisor of a run holds an exclusive `flock` on `<control>/locks/<run id>.lock` for as long
as it lives. The kernel drops the lock when the process dies, so a crashed supervisor never
blocks recovery and a live one is never mistaken for dead. A probe takes a shared lock without
waiting: if that succeeds, nobody holds the exclusive one. Descriptors are not inherited by
child processes, so a worker can never keep a dead supervisor's lock alive.
"""

import fcntl
import os
import threading
import time
from collections.abc import Callable
from pathlib import Path

from agents_dispatcher.domain.run import RunId

FOLDER_MODE = 0o700
FILE_MODE = 0o600
# A status probe holds a shared lock for a moment; `hold` waits this long for it to let go.
HOLD_RETRY_S = 1.0
HOLD_POLL_S = 0.01
# How often a live owner touches its lock file; a reader takes the last touch as a lower bound of
# the time the owner died, so a process that entered a run's folder within this many seconds
# before a crash is held, not killed.
BEAT_S = 5.0


class OwnerLockError(Exception):
    """The run already has a live owner, or its lock file cannot be used."""


class _Beat:
    """Touches the lock file through its descriptor until stopped; `stop` waits for it to end."""

    def __init__(
        self,
        descriptor: int,
        run_id: RunId,
        *,
        interval_s: float,
        touch: Callable[[int], None],
        on_failure: Callable[[str], None] | None,
    ) -> None:
        """Touch once now (the owner is alive), then keep touching in the background."""
        self._descriptor = descriptor
        self._run_id = run_id
        self._touch = touch
        self._on_failure = on_failure
        self._told = False
        self._stopped = threading.Event()
        self._beat()
        self._thread = threading.Thread(
            target=self._run, args=(interval_s,), name=f"owner-beat-{run_id.value}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop beating and wait for a beat in progress, so the descriptor can be closed safely."""
        self._stopped.set()
        self._thread.join()

    def _run(self, interval_s: float) -> None:
        """Beat every `interval_s` until stopped."""
        while not self._stopped.wait(interval_s):
            self._beat()

    def _beat(self) -> None:
        """Touch the file; a failure is told once and never ends the run."""
        try:
            self._touch(self._descriptor)
        except OSError as error:
            if not self._told and self._on_failure is not None:
                self._told = True
                self._on_failure(f"the sign of life of run {self._run_id.value} failed: {error}")


class RunOwnership:
    """The exclusive lock of one run; release it or let the process end."""

    def __init__(self, descriptor: int, beat: _Beat | None = None) -> None:
        """Keep the descriptor that holds the lock and the beat that keeps its file fresh."""
        self._descriptor: int | None = descriptor
        self._beat = beat

    def release(self) -> None:
        """Stop the beat, wait for it, then drop the lock; a second release does nothing."""
        if self._descriptor is not None:
            if self._beat is not None:
                self._beat.stop()
            os.close(self._descriptor)
            self._descriptor = None


class FileRunOwners:
    """Takes and probes the per-run owner locks under one control folder."""

    def __init__(
        self,
        control_dir: str,
        hold_retry_s: float = HOLD_RETRY_S,
        *,
        beat_s: float = BEAT_S,
        touch: Callable[[int], None] = os.utime,
        on_beat_failure: Callable[[str], None] | None = None,
    ) -> None:
        """Keep the control folder, the retry bound and the beat; nothing is created until a lock.

        `touch` marks a held descriptor as touched now; `on_beat_failure` is told once per lock if
        a touch fails.
        """
        self._hold_retry_s = hold_retry_s
        self._beat_s = beat_s
        self._touch = touch
        self._on_beat_failure = on_beat_failure
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
        beat = _Beat(
            descriptor,
            run_id,
            interval_s=self._beat_s,
            touch=self._touch,
            on_failure=self._on_beat_failure,
        )
        return RunOwnership(descriptor, beat)

    def last_alive(self, run_id: RunId) -> float | None:
        """Return when the owner last showed a sign of life, or None when nothing is known.

        The time is a lower bound of the owner's death: it was alive when it last touched the file.
        """
        try:
            return Path(self._path(run_id)).stat().st_mtime
        except FileNotFoundError:
            return None

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
