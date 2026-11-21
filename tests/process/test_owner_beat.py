"""Tests of the sign of life: the lock of a run is touched while its owner lives, never after."""

import os
import tempfile
import threading
import time
import unittest
import uuid
from collections.abc import Callable
from pathlib import Path

from agents_dispatcher.adapters.system.run_owner_lock import FileRunOwners
from agents_dispatcher.domain.run import RunId
from tests.support.hard_timeout import HardTimeoutCase

FAST_BEAT_S = 0.02
WAIT_S = 5.0
OLD = 1234.5
SEVERAL_BEATS = 4


class BeatCase(HardTimeoutCase):
    """A control folder and a run id for each test."""

    def setUp(self) -> None:
        """Create the control folder and the run."""
        super().setUp()
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.control = Path(scratch.name) / "control"
        self.run_id = RunId(str(uuid.uuid4()))
        self.lock_file = self.control / "locks" / f"{self.run_id.value}.lock"

    def age_the_lock(self) -> None:
        """Make the lock file look as if it was last touched long ago."""
        os.utime(self.lock_file, (OLD, OLD))

    @staticmethod
    def wait_for(condition: Callable[[], bool]) -> bool:
        """Poll `condition` for at most `WAIT_S`; return whether it came true."""
        deadline = time.monotonic() + WAIT_S
        while time.monotonic() < deadline:
            if condition():
                return True
            time.sleep(0.01)
        return False


class SignOfLifeTests(BeatCase):
    """What the beat writes and what a reader learns from it."""

    def test_a_lock_that_was_never_taken_has_no_sign_of_life(self) -> None:
        """A missing lock file says nothing about the run."""
        owners = FileRunOwners(str(self.control))
        self.assertIsNone(owners.last_alive(self.run_id), msg="nothing was ever written")

    def test_taking_the_lock_is_a_sign_of_life(self) -> None:
        """The supervisor has been alive at least since it took the lock."""
        before = time.time()
        owners = FileRunOwners(str(self.control), beat_s=1000.0)
        ownership = owners.hold(self.run_id)
        self.addCleanup(ownership.release)
        seen = owners.last_alive(self.run_id)
        self.assertIsNotNone(seen, msg="a time is known")
        self.assertGreaterEqual(seen or 0.0, before - 1.0, msg="and it is recent")

    def test_taking_an_old_lock_again_moves_its_time_at_once(self) -> None:
        """A lock left by an earlier owner shows a fresh sign of life as soon as it is taken."""
        first = FileRunOwners(str(self.control), beat_s=1000.0)
        first.hold(self.run_id).release()
        self.age_the_lock()
        owners = FileRunOwners(str(self.control), beat_s=1000.0)
        ownership = owners.hold(self.run_id)
        self.addCleanup(ownership.release)
        self.assertGreater(
            owners.last_alive(self.run_id) or 0.0, OLD + 1, msg="no later beat was needed"
        )

    def test_the_lock_is_touched_again_while_the_owner_lives(self) -> None:
        """A run that lives on keeps its sign of life fresh."""
        owners = FileRunOwners(str(self.control), beat_s=FAST_BEAT_S)
        ownership = owners.hold(self.run_id)
        self.addCleanup(ownership.release)
        self.age_the_lock()
        fresh = self.wait_for(lambda: (owners.last_alive(self.run_id) or 0.0) > OLD + 1)
        self.assertTrue(fresh, msg="a later beat moved the time forward")

    def test_nothing_touches_the_lock_after_the_release(self) -> None:
        """A released run shows no further sign of life."""
        owners = FileRunOwners(str(self.control), beat_s=FAST_BEAT_S)
        ownership = owners.hold(self.run_id)
        ownership.release()
        self.age_the_lock()
        time.sleep(10 * FAST_BEAT_S)
        self.assertEqual(owners.last_alive(self.run_id), OLD, msg="the file stayed as it was")


class DescriptorSafetyTests(BeatCase):
    """The beat never touches a descriptor that is no longer the lock's."""

    def test_a_release_waits_for_a_beat_in_progress_and_no_beat_follows(self) -> None:
        """The descriptor closes only after the beat that holds it has finished."""
        inside = threading.Event()
        let_go = threading.Event()
        calls: list[int] = []
        open_at_end: list[bool] = []

        def slow_touch(descriptor: int) -> None:
            calls.append(descriptor)
            if len(calls) == 1:
                return  # the touch at the moment the lock is taken is instant
            inside.set()
            let_go.wait(WAIT_S)
            try:
                os.fstat(descriptor)
            except OSError:
                open_at_end.append(False)
            else:
                open_at_end.append(True)

        owners = FileRunOwners(str(self.control), beat_s=FAST_BEAT_S, touch=slow_touch)
        ownership = owners.hold(self.run_id)
        self.assertTrue(inside.wait(WAIT_S), msg="a beat is under way")
        released = threading.Event()

        def release_then_signal() -> None:
            ownership.release()
            released.set()

        releaser = threading.Thread(target=release_then_signal)
        releaser.start()
        self.addCleanup(releaser.join)
        self.assertFalse(released.wait(0.2), msg="the release waits for the beat")
        let_go.set()
        self.assertTrue(released.wait(WAIT_S), msg="and then completes")
        count = len(calls)
        time.sleep(10 * FAST_BEAT_S)
        self.assertEqual(len(calls), count, msg="no beat after the release")
        self.assertTrue(all(open_at_end), msg="the descriptor was open throughout every beat")

    def test_a_beat_that_fails_is_reported_once_and_the_lock_stays_held(self) -> None:
        """A failing touch is told once; it neither ends the run nor stops the beat."""
        attempts: list[int] = []
        told: list[str] = []

        def failing_touch(descriptor: int) -> None:
            attempts.append(descriptor)
            message = "disk said no"
            raise OSError(message)

        owners = FileRunOwners(
            str(self.control),
            beat_s=FAST_BEAT_S,
            touch=failing_touch,
            on_beat_failure=told.append,
        )
        ownership = owners.hold(self.run_id)
        self.addCleanup(ownership.release)
        self.assertTrue(
            self.wait_for(lambda: len(attempts) >= SEVERAL_BEATS), msg="the beat keeps trying"
        )
        self.assertEqual(len(told), 1, msg="the failure is told once")
        self.assertIn("disk said no", told[0], msg="with its cause")
        self.assertIn(self.run_id.value, told[0], msg="and the run it concerns")
        self.assertTrue(owners.owner_alive(self.run_id), msg="the run still has its owner")


if __name__ == "__main__":
    unittest.main()
