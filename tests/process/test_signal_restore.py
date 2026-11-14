"""Tests that signal scopes give back exactly the handlers they found."""

import signal
import unittest
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from types import FrameType
from unittest import mock

from agents_dispatcher.adapters.system.signal_interrupts import SignalInterrupts
from agents_dispatcher.composition.supervisor import HANGUP_SIGNALS, SignalStops
from agents_dispatcher.domain.run import RunId


class NoStops:
    """A stop poll that never asks to stop."""

    def stop_requested(self, run_id: RunId) -> bool:  # noqa: ARG002 - port signature
        """Never."""
        return False

    def stop_reason(self, run_id: RunId) -> str | None:  # noqa: ARG002 - port signature
        """None."""
        return None


def python_handler(number: int, frame: FrameType | None) -> None:
    """Stand in for a handler written in Python; the scopes must put it back as it is."""
    del number, frame


SCOPES: dict[
    str, tuple[tuple[signal.Signals, ...], Callable[[], AbstractContextManager[object]]]
] = {
    "supervisor": (HANGUP_SIGNALS, SignalStops(NoStops()).listening),
    "cli": ((signal.SIGINT, signal.SIGTERM), SignalInterrupts()),
}


class RestoreTests(unittest.TestCase):
    """The handler after the block equals the one before it."""

    @staticmethod
    def put_back(saved: Mapping[signal.Signals, object]) -> None:
        """Give the test process its own handlers back."""
        for number, handler in saved.items():
            signal.signal(number, handler)  # type: ignore[arg-type]

    def test_a_python_handler_is_restored_as_it_was(self) -> None:
        """Both scopes, every signal they take over."""
        for name, (numbers, scope) in SCOPES.items():
            saved = {number: signal.getsignal(number) for number in numbers}
            self.addCleanup(self.put_back, saved)
            for number in numbers:
                signal.signal(number, python_handler)
            with self.subTest(scope=name):
                with scope():
                    pass
                for number in numbers:
                    self.assertIs(
                        signal.getsignal(number), python_handler, msg=f"{name}: {number.name}"
                    )

    def test_a_handler_that_python_cannot_name_is_restored_as_the_default(self) -> None:
        """`signal.signal` returns None for a handler installed from C; None cannot be restored."""
        for name, (numbers, scope) in SCOPES.items():
            calls: list[tuple[int, object]] = []

            def fake(number: int, handler: object, calls: list[tuple[int, object]] = calls) -> None:
                calls.append((number, handler))  # returns None: "installed from C"

            with self.subTest(scope=name), mock.patch("signal.signal", side_effect=fake):
                with scope():
                    pass
                restored = calls[len(numbers) :]
                self.assertEqual(
                    sorted((int(n), h) for n, h in restored),
                    sorted((int(n), signal.SIG_DFL) for n in numbers),
                    msg=f"{name}: every signal goes back to the default",
                )


if __name__ == "__main__":
    unittest.main()
