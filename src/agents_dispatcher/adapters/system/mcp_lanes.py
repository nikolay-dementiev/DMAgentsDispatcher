"""Bounded worker lanes on real threads: one pool and one queue per lane, never shared."""

import threading
import time
from collections import deque
from collections.abc import Callable

from agents_dispatcher.application.lanes import Lane


class _Pool:
    """Workers that take jobs from one bounded queue."""

    def __init__(
        self, *, name: str, workers: int, queue: int, on_job_error: Callable[[BaseException], None]
    ) -> None:
        """Start the workers; `queue` is how many jobs may wait beyond those running."""
        self._queue_limit = queue
        self._on_job_error = on_job_error
        self._jobs: deque[Callable[[], None]] = deque()
        self._closed = False
        self._changed = threading.Condition()
        self._threads = [
            threading.Thread(target=self._work, name=f"{name}-{index}", daemon=True)
            for index in range(workers)
        ]
        for thread in self._threads:
            thread.start()

    def submit(self, job: Callable[[], None]) -> bool:
        """Queue a job; False when full or closed."""
        with self._changed:
            if self._closed or len(self._jobs) >= self._queue_limit:
                return False
            self._jobs.append(job)
            self._changed.notify()
            return True

    def close(self, *, drop_queued: bool) -> None:
        """Stop admission; optionally forget the jobs that have not started."""
        with self._changed:
            self._closed = True
            if drop_queued:
                self._jobs.clear()
            self._changed.notify_all()

    def join(self, deadline: float) -> None:
        """Wait for the workers until the monotonic `deadline`."""
        for thread in self._threads:
            thread.join(max(0.0, deadline - time.monotonic()))

    def _work(self) -> None:
        """Run jobs until closed and empty; a failing job is reported and the worker goes on."""
        while True:
            with self._changed:
                while not self._jobs and not self._closed:
                    self._changed.wait()
                if not self._jobs:
                    return
                job = self._jobs.popleft()
            try:
                job()
            except Exception as error:  # noqa: BLE001 - a job must never kill its worker
                self._on_job_error(error)


class ThreadLanes:
    """The control lane and the normal lane."""

    def __init__(
        self,
        *,
        control_workers: int,
        control_queue: int,
        normal_workers: int,
        normal_queue: int,
        on_job_error: Callable[[BaseException], None],
    ) -> None:
        """Start both pools; `on_job_error` hears every exception a job lets escape."""
        self._pools = {
            Lane.CONTROL: _Pool(
                name="mcp-control",
                workers=control_workers,
                queue=control_queue,
                on_job_error=on_job_error,
            ),
            Lane.NORMAL: _Pool(
                name="mcp-normal",
                workers=normal_workers,
                queue=normal_queue,
                on_job_error=on_job_error,
            ),
        }

    def submit(self, lane: Lane, job: Callable[[], None]) -> bool:
        """Queue a job on a lane; False when that lane is full or closed."""
        return self._pools[lane].submit(job)

    def close(self) -> None:
        """Stop admission and drop the jobs that have not started."""
        for pool in self._pools.values():
            pool.close(drop_queued=True)

    def close_and_join(self, timeout_s: float) -> None:
        """Stop admission, let queued jobs finish, and wait up to `timeout_s` for the workers."""
        deadline = time.monotonic() + timeout_s
        for pool in self._pools.values():
            pool.close(drop_queued=False)
        for pool in self._pools.values():
            pool.join(deadline)
