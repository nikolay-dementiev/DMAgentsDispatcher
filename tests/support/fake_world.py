"""A scripted process table for the containment cleanup: processes, looks and signals in memory."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agents_dispatcher.application.ports import CensusSnapshot, SignalResult
from agents_dispatcher.domain.attribution import ProcessRow
from agents_dispatcher.domain.process import ProcessKey

if TYPE_CHECKING:
    from collections.abc import Callable


@dataclass
class FakeProcess:
    """One process of the fake world and the way it misbehaves."""

    key: ProcessKey
    ppid: int = 1
    pgid: int = 0
    marker: str | None = None
    cwd: str = "/"
    alive: bool = True
    stopped: bool = False
    immortal: bool = False  # a kill is accepted but the process stays
    denied: bool = False  # the signal is refused: it belongs to someone else
    forks: bool = False  # every look that finds it running adds a child


class FakeWorld:
    """Census and signaller over a dictionary; it records every signal in order."""

    def __init__(self, *, incomplete_looks: int = 0) -> None:
        """Start empty; the first `incomplete_looks` looks report an incomplete table."""
        self.processes: dict[int, FakeProcess] = {}
        self.signals: list[tuple[str, int]] = []
        self.looks = 0
        self.incomplete_on: set[int] = set()  # look numbers that cannot be read completely
        self.after_look: Callable[[], None] | None = None  # runs once each look has been taken
        self._incomplete_looks = incomplete_looks
        self._next_pid = 9000

    def add(self, process: FakeProcess) -> FakeProcess:
        """Put a process into the world and return it."""
        self.processes[process.key.pid] = process
        return process

    def replace_identity(self, pid: int, start_time: float) -> None:
        """Make `pid` a different process, as a reused pid would be."""
        self.processes[pid].key = ProcessKey(pid, start_time)

    def look(self, *, paths: bool) -> CensusSnapshot:  # noqa: ARG002 - the fake always shows paths
        """Return the live processes; running forkers add a child first."""
        self.looks += 1
        for process in list(self.processes.values()):
            if process.alive and process.forks and not process.stopped:
                self._fork(process)
        rows = tuple(
            ProcessRow(p.key, p.ppid, p.pgid, p.marker, p.cwd, ())
            for p in self.processes.values()
            if p.alive
        )
        snapshot = CensusSnapshot(
            rows,
            complete=self.looks > self._incomplete_looks and self.looks not in self.incomplete_on,
        )
        if self.after_look is not None:
            self.after_look()
        return snapshot

    def footprint(self, pid: int) -> int | None:
        """Every live fake process weighs 1 MiB."""
        process = self.processes.get(pid)
        return 1_048_576 if process is not None and process.alive else None

    def stop(self, key: ProcessKey) -> SignalResult:
        """Pause the process, if it is still the one named."""
        return self._signal("STOP", key)

    def kill(self, key: ProcessKey) -> SignalResult:
        """End the process, if it is still the one named."""
        return self._signal("KILL", key)

    def _signal(self, name: str, key: ProcessKey) -> SignalResult:
        """Revalidate the identity, then act; nothing is recorded for a refused signal."""
        process = self.processes.get(key.pid)
        if process is None or not process.alive:
            return SignalResult.GONE
        if process.key != key:
            return SignalResult.CHANGED
        if process.denied:
            return SignalResult.DENIED
        self.signals.append((name, key.pid))
        if name == "STOP":
            process.stopped = True
        elif not process.immortal:
            process.alive = False
        return SignalResult.SENT

    def _fork(self, parent: FakeProcess) -> None:
        """Add a child that carries the parent's marker and forks too."""
        self._next_pid += 1
        child = FakeProcess(
            key=ProcessKey(self._next_pid, parent.key.start_time + 1.0),
            ppid=parent.key.pid,
            pgid=self._next_pid,
            marker=parent.marker,
            forks=True,
        )
        self.processes[child.key.pid] = child
