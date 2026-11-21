"""Looks again for the processes of a held run and ends those that belong to it.

Nothing about the run is live any more: its supervisor is gone and its ledger of descendants is
lost. What is left is stored: the run marker, its folders, the group it started and the survivors
recorded when it was quarantined. A process counts as the run's through a folder match only when
it started before the quarantine, so the owner's shell that entered the kept folder afterwards is
never signalled (it can still be attributed as a descendant of a survivor or through the marker).
"""

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from agents_dispatcher.adapters.system.protection import never_signalled
from agents_dispatcher.adapters.system.run_group import verified_group
from agents_dispatcher.adapters.system.seatbelt_confinement import ProcessTable
from agents_dispatcher.adapters.system.sqlite_run_store import StoredRun
from agents_dispatcher.application.containment import CleanupResult, CleanupVerdict
from agents_dispatcher.application.errors import RunStoreError
from agents_dispatcher.application.ports import AbsenceResult
from agents_dispatcher.domain.attribution import RunFacts
from agents_dispatcher.domain.process import ProcessKey
from agents_dispatcher.domain.run import RunId, Survivor


class HeldRuns(Protocol):
    """The store reads the look needs."""

    def get(self, run_id: RunId) -> StoredRun:
        """Return the stored run."""
        ...

    def survivors(self, run_id: RunId) -> list[Survivor]:
        """Return the survivors recorded for the run."""
        ...


class QuarantineLook:
    """An `AbsenceLook` over the cleanup rounds and what the store remembers about the run."""

    def __init__(
        self,
        store: HeldRuns,
        table: ProcessTable,
        *,
        run_area: str,
        containment: Callable[[RunFacts], CleanupResult],
        protected: Callable[[str], frozenset[ProcessKey]],
    ) -> None:
        """Keep the store, the table, the run area and the cleanup to run."""
        self._store = store
        self._table = table
        self._run_area = run_area
        self._containment = containment
        self._protected = protected

    def look(self, run_id: RunId) -> AbsenceResult:
        """Run the cleanup rounds for the run and say whether it was shown empty."""
        run = self._store.get(run_id)
        if run.quarantined_at is None:
            message = f"run {run_id.value} was never quarantined: there is nothing to look for"
            raise RunStoreError(message)
        result = self._containment(self._facts(run, run.quarantined_at))
        return AbsenceResult(
            empty=result.verdict is CleanupVerdict.OBSERVED_EMPTY,
            survivors=result.unresolved(),
            unreadable=tuple(f"pid {key.pid}" for key in result.unreadable),
        )

    def _facts(self, run: StoredRun, quarantined_at: float) -> RunFacts:
        """Collect what is stored about the run, with the upper bound for folder matches."""
        marker = run.run_id.value
        sandbox = Path(self._run_area) / run.run_id.sandbox_name
        group = verified_group(self._table, run.pgid, run.pg_start_time)
        return RunFacts(
            marker=marker,
            shim=ProcessKey(group, max(run.pg_start_time or 0.0, run.started_at)),
            group=group,
            roots=(str(sandbox.resolve()), str((sandbox / "worktree").resolve())),
            remembered=frozenset(
                ProcessKey(item.pid, item.start_time) for item in self._store.survivors(run.run_id)
            ),
            protected_pids=never_signalled(self._table),
            protected_keys=self._protected(marker),
            path_until=quarantined_at,
        )
