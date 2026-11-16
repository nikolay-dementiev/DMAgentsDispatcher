"""One store object used by many threads at once, as a long-lived MCP server does."""

import tempfile
import threading
import time
import unittest
import uuid
from dataclasses import replace
from pathlib import Path

from agents_dispatcher.adapters.system.sqlite_run_store import SqliteRunStore
from agents_dispatcher.application.ports import AcceptedRun
from agents_dispatcher.domain.run import Limits, RunId
from tests.support.routing import ROUTING

THREADS = 8
ROUNDS = 15


class SharedStoreTests(unittest.TestCase):
    """Reads, stop requests, marks and acceptances interleave without a failure."""

    def test_many_threads_share_one_store_without_errors_or_lost_runs(self) -> None:
        """Every thread accepts its own runs while all of them read, stop and mark."""
        failures: list[BaseException] = []
        accepted: list[RunId] = []
        guard = threading.Lock()
        with tempfile.TemporaryDirectory() as directory:
            store = SqliteRunStore(
                str(Path(directory) / "state" / "runs.sqlite3"), epoch_s=time.time, busy_s=20.0
            )

            def work() -> None:
                try:
                    for _ in range(ROUNDS):
                        run_id = RunId(str(uuid.uuid4()))
                        run = AcceptedRun(
                            run_id, "/r", "x", Limits(60.0, 1024), replace(ROUTING, provider="p")
                        )
                        store.accept(run, capacity=None)
                        with guard:
                            accepted.append(run_id)
                        store.request_stop(run_id, "test")
                        store.find(run_id)
                        store.active("p")
                        store.unfinished()
                        store.mark_lost("p")
                        store.is_lost("p")
                        store.clear_lost("p")
                except BaseException as error:  # noqa: BLE001 - collected, asserted below
                    with guard:
                        failures.append(error)

            threads = [threading.Thread(target=work) for _ in range(THREADS)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=120)
            found = [store.find(run_id) for run_id in accepted]
        self.assertEqual(failures, [], msg="no thread met an error")
        self.assertEqual(len(accepted), THREADS * ROUNDS, msg="every acceptance returned")
        self.assertNotIn(None, found, msg="every accepted run is in the store")


if __name__ == "__main__":
    unittest.main()
