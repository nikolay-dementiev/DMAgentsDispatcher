"""Tests of the bounded worker lanes, with real threads."""

import threading
import unittest

from agents_dispatcher.adapters.system.mcp_lanes import ThreadLanes
from agents_dispatcher.interfaces.mcp.ports import Lane

WAIT_S = 10.0


def make_lanes(errors: list[BaseException] | None = None, **sizes: int) -> ThreadLanes:
    """Build lanes; unspecified sizes are 1 worker and a queue of 1."""
    values = {"control_workers": 1, "control_queue": 1, "normal_workers": 1, "normal_queue": 1}
    values.update(sizes)
    sink = errors if errors is not None else []
    return ThreadLanes(on_job_error=sink.append, **values)


class LaneTests(unittest.TestCase):
    """Jobs run off the caller's thread, within bounds, each lane on its own."""

    def setUp(self) -> None:
        """Track lanes so every test closes its threads."""
        self.lanes: list[ThreadLanes] = []

    def tearDown(self) -> None:
        """Stop the lanes the test made."""
        for lanes in self.lanes:
            lanes.close()

    def build(self, errors: list[BaseException] | None = None, **sizes: int) -> ThreadLanes:
        """Build and remember lanes."""
        lanes = make_lanes(errors, **sizes)
        self.lanes.append(lanes)
        return lanes

    def test_a_job_runs_on_a_worker_thread(self) -> None:
        """Not on the thread that submitted it."""
        lanes = self.build()
        done = threading.Event()
        names: list[str] = []

        def job() -> None:
            names.append(threading.current_thread().name)
            done.set()

        self.assertTrue(lanes.submit(Lane.NORMAL, job), msg="accepted")
        self.assertTrue(done.wait(WAIT_S), msg="ran")
        self.assertNotEqual(names[0], threading.current_thread().name, msg="another thread")

    def test_a_full_queue_refuses_and_room_returns_when_work_finishes(self) -> None:
        """One worker, one queued job: the third is refused."""
        lanes = self.build()
        release = threading.Event()
        started = threading.Event()
        finished: list[str] = []

        def blocker() -> None:
            started.set()
            release.wait(WAIT_S)
            finished.append("a")

        self.assertTrue(lanes.submit(Lane.NORMAL, blocker), msg="running")
        self.assertTrue(started.wait(WAIT_S), msg="the worker holds it")
        self.assertTrue(lanes.submit(Lane.NORMAL, lambda: finished.append("b")), msg="queued")
        self.assertFalse(lanes.submit(Lane.NORMAL, lambda: finished.append("c")), msg="refused")
        release.set()
        lanes.close_and_join(WAIT_S)
        self.assertEqual(finished, ["a", "b"], msg="the refused job never ran")

    def test_a_full_normal_lane_does_not_block_the_control_lane(self) -> None:
        """Separate queues and workers."""
        lanes = self.build()
        release = threading.Event()
        started = threading.Event()
        control_done = threading.Event()

        def blocker() -> None:
            started.set()
            release.wait(WAIT_S)

        lanes.submit(Lane.NORMAL, blocker)
        started.wait(WAIT_S)
        lanes.submit(Lane.NORMAL, lambda: None)
        self.assertFalse(lanes.submit(Lane.NORMAL, lambda: None), msg="normal is full")
        self.assertTrue(lanes.submit(Lane.CONTROL, control_done.set), msg="control accepts")
        self.assertTrue(control_done.wait(WAIT_S), msg="and runs while normal is stuck")
        release.set()

    def test_workers_of_a_lane_run_in_parallel(self) -> None:
        """Two workers, two jobs that wait for each other."""
        lanes = self.build(normal_workers=2)
        barrier = threading.Barrier(2, timeout=WAIT_S)
        passed: list[int] = []

        def job() -> None:
            barrier.wait()
            passed.append(1)

        lanes.submit(Lane.NORMAL, job)
        lanes.submit(Lane.NORMAL, job)
        lanes.close_and_join(WAIT_S)
        self.assertEqual(passed, [1, 1], msg="both reached the barrier together")

    def test_a_job_that_raises_is_reported_and_the_worker_keeps_going(self) -> None:
        """No silent loss, no dead worker."""
        errors: list[BaseException] = []
        lanes = self.build(errors)
        ran = threading.Event()

        def bad() -> None:
            message = "boom"
            raise RuntimeError(message)

        lanes.submit(Lane.NORMAL, bad)
        lanes.submit(Lane.NORMAL, ran.set)
        self.assertTrue(ran.wait(WAIT_S), msg="the next job still ran")
        self.assertEqual([str(error) for error in errors], ["boom"], msg="reported")

    def test_after_close_nothing_is_accepted_and_queued_jobs_are_dropped(self) -> None:
        """Shutdown stops admission and abandons what has not started."""
        lanes = self.build()
        release = threading.Event()
        started = threading.Event()
        ran: list[str] = []

        def blocker() -> None:
            started.set()
            release.wait(WAIT_S)

        lanes.submit(Lane.NORMAL, blocker)
        started.wait(WAIT_S)
        lanes.submit(Lane.NORMAL, lambda: ran.append("queued"))
        lanes.close()
        self.assertFalse(lanes.submit(Lane.CONTROL, lambda: ran.append("late")), msg="closed")
        release.set()
        lanes.close_and_join(WAIT_S)
        self.assertEqual(ran, [], msg="the queued job was dropped")


if __name__ == "__main__":
    unittest.main()
