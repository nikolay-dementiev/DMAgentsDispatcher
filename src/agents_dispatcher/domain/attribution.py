"""Which processes belong to a run (O1 layer 3): four sources and the guards around them.

Pure: the caller reads the process table and passes rows in; nothing here signals or reads a file.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum

from agents_dispatcher.domain.process import ProcessKey


class Source(Enum):
    """Why a process is attributed to a run."""

    GROUP = "GROUP"  # (a) in the run's process group
    DESCENDANT = "DESCENDANT"  # (b) observed as a descendant of the shim or an attributed process
    MARKER = "MARKER"  # (c) carries the inherited run marker
    PATH = "PATH"  # (d) its cwd or an open file lies inside the run area


@dataclass(frozen=True)
class ProcessRow:
    """What one look at the table showed about one process."""

    key: ProcessKey
    ppid: int
    pgid: int
    marker: str | None
    cwd: str | None
    open_paths: tuple[str, ...]
    descriptors: int
    paths_denied: bool = False  # the cwd or the open files were refused; what was read is kept


@dataclass(frozen=True)
class RunFacts:
    """What is known about the run before the look."""

    marker: str
    shim: ProcessKey
    group: int
    roots: tuple[str, ...]  # real paths of the sandbox and the worktree
    remembered: frozenset[ProcessKey]  # descendants seen earlier
    protected_pids: frozenset[int]  # pid 0 and 1, this process and its ancestors
    protected_keys: frozenset[ProcessKey]  # identities that other live runs own
    path_until: float | None  # a re-check ignores path matches that started after this time


@dataclass(frozen=True)
class Attributed:
    """A process that belongs to the run, and why."""

    key: ProcessKey
    sources: frozenset[Source]


@dataclass(frozen=True)
class Attribution:
    """The result of one look: targets, path matches that must not be signalled, the ledger."""

    found: tuple[Attributed, ...]
    residual: tuple[ProcessKey, ...]
    remembered: frozenset[ProcessKey]
    unreadable: tuple[ProcessKey, ...] = ()  # later, unattributed, paths refused: not ruled out


def attribute(rows: Iterable[ProcessRow], facts: RunFacts) -> Attribution:
    """Decide which of `rows` belong to the run described by `facts`."""
    candidates = [row for row in rows if not _protected(row, facts)]
    descendants = _descendants(candidates, facts)
    found: list[Attributed] = []
    residual: list[ProcessKey] = []
    unreadable: list[ProcessKey] = []
    for row in candidates:
        sources = _sources(row, facts, descendants)
        if _inside(row, facts.roots):
            if _path_may_attribute(row, facts):
                sources.add(Source.PATH)
            elif not sources:
                residual.append(row.key)
        elif row.paths_denied and not sources and _path_may_attribute(row, facts):
            unreadable.append(row.key)
        if sources:
            found.append(Attributed(row.key, frozenset(sources)))
    found.sort(key=lambda item: item.key.pid)
    keys = frozenset(item.key for item in found)
    return Attribution(tuple(found), tuple(residual), facts.remembered | keys, tuple(unreadable))


def _protected(row: ProcessRow, facts: RunFacts) -> bool:
    """Return whether this process must never be attributed or signalled."""
    return row.key.pid in facts.protected_pids or row.key in facts.protected_keys


def _sources(row: ProcessRow, facts: RunFacts, descendants: set[int]) -> set[Source]:
    """Return the sources (a) to (c) that attribute the row."""
    sources: set[Source] = set()
    if row.pgid == facts.group:
        sources.add(Source.GROUP)
    if row.key in facts.remembered or row.key.pid in descendants:
        sources.add(Source.DESCENDANT)
    if row.marker == facts.marker:
        sources.add(Source.MARKER)
    return sources


def _descendants(rows: list[ProcessRow], facts: RunFacts) -> set[int]:
    """Return the pids whose parent chain leads to the shim or to a remembered process.

    A remembered process is a start of the chain by its key, so a reused pid starts none.
    """
    reached = {facts.shim.pid} | {row.key.pid for row in rows if row.key in facts.remembered}
    found: set[int] = set()
    changed = True
    while changed:
        changed = False
        for row in rows:
            if row.key.pid in found or row.key.start_time < facts.shim.start_time:
                continue
            if row.ppid in reached:
                found.add(row.key.pid)
                reached.add(row.key.pid)
                changed = True
    return found


def _inside(row: ProcessRow, roots: tuple[str, ...]) -> bool:
    """Return whether the cwd or any open file lies inside a root (whole path components)."""
    paths = (row.cwd, *row.open_paths)
    return any(path is not None and _within(path, root) for path in paths for root in roots)


def _within(path: str, root: str) -> bool:
    """Return whether `path` is `root` or lies below it, comparing whole components."""
    return path == root or path.startswith(root.rstrip("/") + "/")


def _path_may_attribute(row: ProcessRow, facts: RunFacts) -> bool:
    """Apply the guard: a path match counts only for a process that started with or after the shim.

    A re-check also ignores a process that started after the run was quarantined.
    """
    if row.key.start_time < facts.shim.start_time:
        return False
    return facts.path_until is None or row.key.start_time <= facts.path_until
