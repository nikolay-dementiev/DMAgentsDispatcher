"""Real-process tests of the re-check of a held run: it ends leftovers, never a bystander."""

import os
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from agents_dispatcher.adapters.system.process_table import LibprocProcessTable
from agents_dispatcher.adapters.system.quarantine_look import QuarantineLook
from agents_dispatcher.adapters.system.sqlite_run_store import SqliteRunStore
from agents_dispatcher.application.ports import AcceptedRun, FinishRecord, ProcessIdentity
from agents_dispatcher.domain.run import Limits, RunId, RunState, Survivor, TerminalReason
from tests.support.confinement_kit import nobody_protected, real_cleanup
from tests.support.hard_timeout import HardTimeoutCase
from tests.support.narrow import present
from tests.support.routing import ROUTING
from tests.support.start_times import start_time_of

SLEEPER = "import time; time.sleep(300)"
KEPT = FinishRecord(TerminalReason.COMPLETED, None, 0, None, 0, 3.5, "{}")
WAIT_S = 10.0


def alive(pid: int) -> bool:
    """Return whether a process with this pid exists."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


class RecheckTests(HardTimeoutCase):
    """What a re-check does to a process that belongs to a held run and to one that does not."""

    def setUp(self) -> None:
        """Open a store and a run area; keep a table."""
        super().setUp()
        self.run_id = RunId(str(uuid.uuid4()))
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.table = LibprocProcessTable()
        self.run_area = Path(scratch.name).resolve() / "runs"
        self.sandbox = self.run_area / self.run_id.sandbox_name
        (self.sandbox / "worktree").mkdir(parents=True)
        self.store = SqliteRunStore(
            str(Path(scratch.name) / "runs.sqlite3"), epoch_s=time.time, busy_s=2.0
        )
        self.children: list[subprocess.Popen[bytes]] = []
        self.addCleanup(self.reap)

    def reap(self) -> None:
        """Kill and reap every test child."""
        for child in self.children:
            child.kill()
            child.wait(timeout=WAIT_S)

    def spawn(self, *, marker: bool, cwd: Path | None = None) -> subprocess.Popen[bytes]:
        """Start a sleeper in its own session, optionally with the run marker."""
        env = {**os.environ, **({"AGENTSD_RUN_ID": self.run_id.value} if marker else {})}
        child = subprocess.Popen(  # noqa: S603 - argv array of this interpreter, no shell
            [sys.executable, "-B", "-c", SLEEPER], start_new_session=True, env=env, cwd=cwd
        )
        self.children.append(child)
        deadline = time.monotonic() + WAIT_S
        while start_time_of(self.table, child.pid) is None and time.monotonic() < deadline:
            time.sleep(0.05)
        return child

    def quarantine(self, survivor: subprocess.Popen[bytes]) -> None:
        """Hold the run with `survivor` recorded as its leftover."""
        self.store.accept(
            AcceptedRun(self.run_id, "/work/example", "t", Limits(60.0, 1024), ROUTING)
        )
        self.store.claim(self.run_id, ProcessIdentity(pgid=1, start_time_s=None))
        self.store.set_state(self.run_id, RunState.FINALIZING)
        start = present(start_time_of(self.table, survivor.pid))
        self.store.quarantine(self.run_id, KEPT, [Survivor(survivor.pid, start, ("marker",))])

    def look(self) -> QuarantineLook:
        """Build the look over the real cleanup rounds."""
        return QuarantineLook(
            self.store,
            self.table,
            run_area=str(self.run_area),
            containment=real_cleanup(self.table).run,
            protected=nobody_protected,
        )

    def test_a_recorded_survivor_is_ended_and_the_run_is_shown_empty(self) -> None:
        """The second chance works: what was alive at the quarantine is gone after the look."""
        survivor = self.spawn(marker=True)
        self.quarantine(survivor)
        found = self.look().look(self.run_id)
        survivor.wait(timeout=WAIT_S)
        self.assertFalse(alive(survivor.pid), msg="the survivor was ended")
        self.assertTrue(found.empty, msg="and the run is shown empty")

    def test_a_process_started_after_the_quarantine_in_the_kept_folder_is_never_signalled(
        self,
    ) -> None:
        """A3: the owner's shell that entered the folder later is not the run's."""
        survivor = self.spawn(marker=True)
        self.quarantine(survivor)
        time.sleep(0.3)
        bystander = self.spawn(marker=False, cwd=self.sandbox)
        found = self.look().look(self.run_id)
        self.assertIsNone(bystander.poll(), msg="the bystander was not touched")
        self.assertTrue(alive(bystander.pid), msg="and is alive")
        self.assertTrue(found.empty, msg="it does not keep the run from being shown empty")


if __name__ == "__main__":
    unittest.main()
