"""Tests of the signal scope behind `wait` and `run`: real signals sent to this process."""

import os
import signal
import time
import unittest

from agents_dispatcher.adapters.system.signal_interrupts import SignalInterrupts
from agents_dispatcher.interfaces.cli.services import InterruptFlag

HANDLED = (signal.SIGINT, signal.SIGTERM)


def wait_until_raised(flag: InterruptFlag, seconds: float = 5.0) -> bool:
    """Poll the flag; a handler runs between bytecodes, so the signal may need a moment."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if flag.raised():
            return True
        time.sleep(0.01)
    return False


class SignalInterruptsTests(unittest.TestCase):
    """The flag rises on SIGINT and SIGTERM, and the old handlers come back."""

    def test_the_flag_is_down_until_a_signal_arrives(self) -> None:
        """Entering the scope alone raises nothing."""
        with SignalInterrupts()() as flag:
            self.assertFalse(flag.raised(), msg="no signal yet")

    def test_each_handled_signal_raises_the_flag_without_ending_the_process(self) -> None:
        """The process survives, which is what lets `run` keep waiting."""
        for number in HANDLED:
            with self.subTest(signal=number.name), SignalInterrupts()() as flag:
                os.kill(os.getpid(), number)
                self.assertTrue(wait_until_raised(flag), msg=f"{number.name} raised the flag")

    def test_the_previous_handlers_are_restored_on_exit(self) -> None:
        """A scope leaves the process as it found it, also after an error inside."""
        before = {number: signal.getsignal(number) for number in HANDLED}
        with SignalInterrupts()():
            for number in HANDLED:
                self.assertNotEqual(
                    signal.getsignal(number), before[number], msg=f"{number.name} is handled"
                )
        with self.assertRaises(RuntimeError), SignalInterrupts()():
            message = "boom"
            raise RuntimeError(message)
        for number in HANDLED:
            self.assertEqual(
                signal.getsignal(number), before[number], msg=f"{number.name} restored"
            )

    def test_the_flag_counts_how_many_signals_arrived(self) -> None:
        """`run` needs to tell a first interrupt from a second one."""
        with SignalInterrupts()() as flag:
            self.assertEqual(flag.count(), 0, msg="none yet")
            os.kill(os.getpid(), signal.SIGINT)
            self.assertTrue(wait_until_raised(flag), msg="first arrived")
            self.assertEqual(flag.count(), 1, msg="one signal")
            os.kill(os.getpid(), signal.SIGTERM)
            deadline = time.monotonic() + 5.0
            while flag.count() < 2 and time.monotonic() < deadline:  # noqa: PLR2004 - two signals
                time.sleep(0.01)
            self.assertEqual(flag.count(), 2, msg="a second signal of either kind counts")

    def test_two_scopes_do_not_share_a_flag(self) -> None:
        """A flag raised in one scope does not leak into the next."""
        with SignalInterrupts()() as first:
            os.kill(os.getpid(), signal.SIGTERM)
            self.assertTrue(wait_until_raised(first), msg="first scope raised")
        with SignalInterrupts()() as second:
            self.assertFalse(second.raised(), msg="second scope starts down")


if __name__ == "__main__":
    unittest.main()
