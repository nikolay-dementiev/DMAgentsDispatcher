"""Tests of the pure attribution rules of the O1 layer 3: which processes belong to a run."""

import unittest
from collections.abc import Iterable

from agents_dispatcher.domain.attribution import (
    Attributed,
    ProcessRow,
    RunFacts,
    Source,
    attribute,
)
from agents_dispatcher.domain.process import ProcessKey

SANDBOX = "/state/runs/run-1"
WORKTREE = "/state/runs/run-1/worktree"
SHIM = 100
SHIM_START = 1000.0
LATER = 1001.0
EARLIER = 900.0
MARKER = "run-1"


def row(  # noqa: PLR0913 - a table row has exactly these facts
    pid: int,
    *,
    ppid: int = 1,
    pgid: int | None = None,
    start: float = LATER,
    marker: str | None = None,
    cwd: str = "/",
    files: tuple[str, ...] = (),
) -> ProcessRow:
    """Build a process row with neutral defaults: not in the group, no marker, cwd elsewhere."""
    return ProcessRow(
        key=ProcessKey(pid, start),
        ppid=ppid,
        pgid=pid if pgid is None else pgid,
        marker=marker,
        cwd=cwd,
        open_paths=files,
    )


def facts(
    *,
    remembered: frozenset[ProcessKey] = frozenset(),
    protected_pids: frozenset[int] = frozenset({0, 1, 4242}),
    protected_keys: frozenset[ProcessKey] = frozenset(),
    until: float | None = None,
) -> RunFacts:
    """Build the facts of run-1: shim 100 started at 1000.0, its group is 100."""
    return RunFacts(
        marker=MARKER,
        shim=ProcessKey(SHIM, SHIM_START),
        group=SHIM,
        roots=(SANDBOX, WORKTREE),
        remembered=remembered,
        protected_pids=protected_pids,
        protected_keys=protected_keys,
        path_until=until,
    )


def sources_of(found: Iterable[Attributed], pid: int) -> frozenset[Source]:
    """Return the sources that attributed `pid`, or an empty set."""
    for item in found:
        if item.key.pid == pid:
            return frozenset(item.sources)
    return frozenset()


class AttributionTests(unittest.TestCase):
    """The four sources of attribution and the guards around them."""

    def test_a_member_of_the_run_group_is_attributed(self) -> None:
        """Source (a): the process group of the shim."""
        result = attribute([row(200, pgid=SHIM)], facts())
        self.assertEqual(sources_of(result.found, 200), {Source.GROUP}, "found by its group")

    def test_a_child_of_the_shim_is_a_descendant_even_outside_the_group(self) -> None:
        """Source (b): the parent chain leads to the shim, a grandchild leads through the child."""
        rows = [row(200, ppid=SHIM), row(300, ppid=200)]
        result = attribute(rows, facts())
        self.assertIn(Source.DESCENDANT, sources_of(result.found, 200), "the child of the shim")
        self.assertIn(Source.DESCENDANT, sources_of(result.found, 300), "the grandchild")

    def test_a_remembered_descendant_stays_attributed_after_it_hides(self) -> None:
        """Source (b): pid plus start time is remembered, so a reparent or chdir changes nothing."""
        hidden = row(300, ppid=1, pgid=300, cwd="/")
        remembered = frozenset({ProcessKey(300, LATER)})
        result = attribute([hidden], facts(remembered=remembered))
        self.assertEqual(sources_of(result.found, 300), {Source.DESCENDANT}, "remembered")

    def test_a_reused_pid_is_not_the_remembered_process(self) -> None:
        """A remembered pid whose start time differs is another process."""
        stranger = row(300, start=LATER + 50)
        remembered = frozenset({ProcessKey(300, LATER)})
        result = attribute([stranger], facts(remembered=remembered))
        self.assertEqual(sources_of(result.found, 300), frozenset(), "pid reuse is not attributed")

    def test_the_marker_attributes_a_process_in_its_own_group(self) -> None:
        """Source (c): the inherited marker."""
        result = attribute([row(200, marker=MARKER)], facts())
        self.assertEqual(sources_of(result.found, 200), {Source.MARKER}, "found by marker")

    def test_another_runs_marker_does_not_attribute(self) -> None:
        """The marker must carry this run's value."""
        result = attribute([row(200, marker="run-2")], facts())
        self.assertEqual(sources_of(result.found, 200), frozenset(), "not this run's marker")

    def test_a_cwd_in_the_sandbox_attributes_a_younger_process(self) -> None:
        """Source (d): the working directory inside the run area."""
        result = attribute([row(200, cwd=SANDBOX + "/sub")], facts())
        self.assertEqual(sources_of(result.found, 200), {Source.PATH}, "found by cwd")

    def test_an_open_file_in_the_worktree_attributes_a_younger_process(self) -> None:
        """Source (d): any open file inside the run area."""
        result = attribute([row(200, files=("/other/x", WORKTREE + "/a.py"))], facts())
        self.assertEqual(sources_of(result.found, 200), {Source.PATH}, "found by open file")

    def test_a_sibling_folder_with_the_same_prefix_is_not_inside(self) -> None:
        """Prefix by whole path component: run-10 is not inside run-1."""
        result = attribute([row(200, cwd="/state/runs/run-10")], facts())
        self.assertEqual(sources_of(result.found, 200), frozenset(), "run-10 is not run-1")

    def test_an_older_process_with_a_path_match_is_a_residual_not_a_target(self) -> None:
        """The guard: a shell that was there before the shim is reported, never signalled."""
        result = attribute([row(200, start=EARLIER, cwd=SANDBOX)], facts())
        self.assertEqual(sources_of(result.found, 200), frozenset(), "never a target")
        self.assertEqual([item.pid for item in result.residual], [200], "reported as a residual")

    def test_a_process_started_after_the_limit_is_a_residual(self) -> None:
        """A re-check ignores path matches that started after quarantine (the owner's shell)."""
        result = attribute([row(200, start=LATER + 10, cwd=SANDBOX)], facts(until=LATER + 5))
        self.assertEqual(sources_of(result.found, 200), frozenset(), "after quarantine")
        self.assertEqual([item.pid for item in result.residual], [200], "reported as a residual")

    def test_the_protected_pids_are_never_found(self) -> None:
        """Pid 1, the SDK's own process and its ancestors are never targets."""
        rows = [
            row(1, marker=MARKER, pgid=SHIM),
            row(4242, marker=MARKER, cwd=SANDBOX),
        ]
        result = attribute(rows, facts())
        self.assertEqual(result.found, (), "nothing protected is found")

    def test_another_runs_identity_is_never_attributed_through_a_path(self) -> None:
        """Run B's supervisor with its cwd in run A's sandbox is not run A's process."""
        other = row(500, cwd=SANDBOX)
        result = attribute([other], facts(protected_keys=frozenset({other.key})))
        self.assertEqual(result.found, (), "protected identity is skipped")

    def test_found_processes_are_remembered_for_the_next_look(self) -> None:
        """The result carries every attributed key, so the caller can keep the ledger."""
        result = attribute([row(200, ppid=SHIM)], facts())
        self.assertIn(ProcessKey(200, LATER), result.remembered, "the child is remembered")


if __name__ == "__main__":
    unittest.main()
