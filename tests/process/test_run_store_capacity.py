"""Two real processes race for the last slot of a provider: exactly one run is accepted."""

import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from agents_dispatcher.adapters.system.sqlite_run_store import SqliteRunStore

RACERS = 4
CAPACITY = 1
SCRIPT = textwrap.dedent(
    """
    import sys, time
    from dataclasses import replace
    from agents_dispatcher.adapters.system.sqlite_run_store import SqliteRunStore
    from agents_dispatcher.application.errors import CapacityContendedError
    from agents_dispatcher.application.ports import AcceptedRun
    from agents_dispatcher.domain.run import Limits, RunId
    from tests.support.routing import ROUTING

    path, run_id, go_at = sys.argv[1], sys.argv[2], float(sys.argv[3])
    store = SqliteRunStore(path, epoch_s=time.time, busy_s=20.0)
    while time.time() < go_at:
        pass
    run = AcceptedRun(RunId(run_id), "/r", "x", Limits(60.0, 1024), replace(ROUTING, provider="p"))
    try:
        store.accept(run, capacity=1)
    except CapacityContendedError:
        print("full")
    else:
        print("taken")
    """
)


class SlotRaceTests(unittest.TestCase):
    """The count and the insert are one transaction, so the last slot goes to one writer."""

    def test_concurrent_processes_never_both_take_the_last_slot(self) -> None:
        """Real processes released at the same instant; the store file is the only referee."""
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state" / "runs.sqlite3")
            SqliteRunStore(path, epoch_s=time.time, busy_s=20.0)
            go_at = time.time() + 3.0
            ids = [f"7a1c9e52-3b64-4d08-8f21-0c5d9a4e6b{n:02d}" for n in range(RACERS)]
            children = [
                subprocess.Popen(  # noqa: S603 - a fixed script, argument array
                    [sys.executable, "-c", SCRIPT, path, run_id, str(go_at)],
                    stdout=subprocess.PIPE,
                    text=True,
                    env={"PYTHONPATH": ":".join(sys.path)},
                )
                for run_id in ids
            ]
            answers = [child.communicate(timeout=60)[0].strip() for child in children]
        self.assertEqual(answers.count("taken"), CAPACITY, msg=f"one winner: {answers}")
        self.assertEqual(
            answers.count("full"), RACERS - CAPACITY, msg=f"the rest refused: {answers}"
        )
