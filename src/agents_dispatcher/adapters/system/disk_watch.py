"""The disk watch: walks the run area on its own thread and lets the watch loop judge the result.

A walk of a large tree can take longer than a watch scan, so it must not run on the loop's
thread. The loop therefore never trusts the thread to report its own trouble: it reads the last
result together with the loop's clock and decides whether that result is still fresh enough.
"""

import threading
from collections.abc import Sequence
from dataclasses import dataclass

from agents_dispatcher.application.ports import Clock, DiskUsage, DiskWalk
from agents_dispatcher.application.settings import WalkSettings

JOIN_S = 2.0  # a walk stuck in the kernel is left behind: the thread is a daemon


@dataclass(frozen=True)
class DiskReading:
    """The last usage the walk found and, when the disk can no longer be measured, why."""

    usage: DiskUsage | None
    lost: str | None


class DiskWatch:
    """Runs a `DiskWalk` over the roots again and again and keeps its latest answer."""

    def __init__(
        self, walk: DiskWalk, clock: Clock, settings: WalkSettings, roots: Sequence[str]
    ) -> None:
        """Keep the walk, the loop's clock, the bounds and the roots to measure."""
        self._walk = walk
        self._clock = clock
        self._settings = settings
        self._roots = tuple(roots)
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._begun = 0.0
        self._ended_at: float | None = None
        self._usage: DiskUsage | None = None
        self._failure: str | None = None

    def start(self) -> None:
        """Begin walking; the age of a result is counted from this moment until one arrives."""
        self._begun = self._clock.now()
        self._thread = threading.Thread(target=self._loop, name="disk-walk", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Ask the thread to end and wait a short while for it."""
        self._stopping.set()
        if self._thread is not None:
            self._thread.join(timeout=JOIN_S)

    def reading(self) -> DiskReading:
        """Return the latest usage, and the loss when the walk failed or is too old."""
        with self._lock:
            reference = self._begun if self._ended_at is None else self._ended_at
            age = self._clock.now() - reference
            lost = self._failure
            if lost is None and age > self._settings.maximum_age_s:
                lost = (
                    f"the disk walk has produced no result for {age:.1f} s "
                    f"(maximum {self._settings.maximum_age_s:g} s)"
                )
            return DiskReading(self._usage, lost)

    def _loop(self) -> None:
        """Walk, record, wait; every failure is recorded, none ends the thread silently."""
        while not self._stopping.is_set():
            try:
                usage = self._walk.measure(self._roots)
            except Exception as error:  # noqa: BLE001 - any failure of a walk means "not measured"
                self._record(None, f"the disk walk failed: {type(error).__name__}: {error}")
            else:
                self._record(usage, None)
            self._stopping.wait(self._settings.interval_s)

    def _record(self, usage: DiskUsage | None, failure: str | None) -> None:
        """Store the outcome of one walk with the time it ended."""
        with self._lock:
            self._usage = usage
            self._failure = failure
            self._ended_at = self._clock.now()
